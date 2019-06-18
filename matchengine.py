import argparse
import asyncio
import glob
import json
import logging
import os
import sys
from collections import deque, defaultdict
from multiprocessing import cpu_count
from typing import Generator

import networkx as nx
from pymongo import UpdateMany, InsertOne
from pymongo.errors import AutoReconnect, CursorNotFound

import query_transform
from load import load
from match_criteria_transform import MatchCriteriaTransform, query_node_transform
from mongo_connection import MongoDBConnection
from matchengine_types import *
from query_transform import QueryTransformerContainer
from trial_match_utils import *

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('matchengine')


def check_indices():
    """
    Ensure indexes exist on the trial_match collection so queries are performant
    """
    with MongoDBConnection(read_only=False, async_init=False) as db:
        indexes = db.trial_match_raw.list_indexes()
        existing_indexes = set()
        desired_indexes = {'hash', 'mrn', 'sample_id', 'clinical_id', 'protocol_no'}
        for index in indexes:
            index_key = list(index['key'].to_dict().keys())[0]
            existing_indexes.add(index_key)
        indexes_to_create = desired_indexes - existing_indexes
        for index in indexes_to_create:
            log.info('Creating index %s' % index)
            db.trial_match_raw.create_index(index)


class MatchEngine(object):
    cache: Cache
    config: Dict
    match_criteria_transform: MatchCriteriaTransform
    protocol_nos: Union[List[str], None]
    sample_ids: Union[List[str], None]
    match_on_closed: bool
    match_on_deceased: bool
    debug: bool
    num_workers: int
    clinical_ids: Set[ClinicalID]
    _task_q: asyncio.queues.Queue
    matches: Dict[str, Dict[str, List[Dict]]]
    _loop: asyncio.AbstractEventLoop
    _queue_task_count: int
    _workers: Dict[int, asyncio.Task]
    run_log: RunLog

    def __enter__(self):
        return self

    async def _async_exit(self):
        """
        Ensure that all async workers exit gracefully.
        """
        for _ in range(0, self.num_workers):
            await self._task_q.put(PoisonPill())
        await self._task_q.join()

    def __exit__(self, exception_type, exception_value, exception_traceback):
        """
        Teardown database connections (async + synchronous) and async workers gracefully.
        """
        self._async_db_ro.__exit__(exception_type, exception_value, exception_traceback)
        self._async_db_rw.__exit__(exception_type, exception_value, exception_traceback)
        self._db_ro.__exit__(exception_type, exception_value, exception_traceback)
        self._loop.run_until_complete(self._async_exit())
        self._loop.stop()

    def __init__(self,
                 cache: Cache = None, sample_ids: Set[str] = None, protocol_nos: Set[str] = None,
                 match_on_deceased: bool = False, match_on_closed: bool = False, debug: bool = False,
                 num_workers: int = cpu_count() * 5, visualize_match_paths: bool = False, fig_dir: str = None,
                 config_path: str = None, plugin_dir: str = None):

        with open(config_path) as config_file_handle:
            self.config = json.load(config_file_handle)
        self.match_criteria_transform = MatchCriteriaTransform(self.config)
        self.plugin_dir = plugin_dir
        self._find_plugins()
        self._db_ro = MongoDBConnection(read_only=True, async_init=False)
        self.db_ro = self._db_ro.__enter__()
        self._db_rw = MongoDBConnection(read_only=False, async_init=False)
        self.db_rw = self._db_rw.__enter__()

        # A cache-like object used to accumulate query results
        self.cache = Cache() if cache is None else cache
        self.sample_ids = sample_ids
        self.protocol_nos = protocol_nos
        self.match_on_closed = match_on_closed
        self.match_on_deceased = match_on_deceased
        self.debug = debug
        self.num_workers = num_workers
        self.visualize_match_paths = visualize_match_paths
        self.fig_dir = fig_dir
        self._queue_task_count = int()
        self.matches = defaultdict(lambda: defaultdict(list))

        self.trials = self.get_trials()
        if self.protocol_nos is None:
            self.protocol_nos = list(self.trials.keys())
        self.clinical_mapping = self.get_clinical_ids_from_sample_ids()
        self.sample_mapping = {sample_id: clinical_id for clinical_id, sample_id in self.clinical_mapping.items()}
        self.clinical_ids = set(self.clinical_mapping.keys())
        if self.sample_ids is None:
            self.sample_ids = list(self.clinical_mapping.values())

        # instantiate a new async event loop to allow class to be used as if it is synchronous
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_init())

    def _find_plugins(self):
        """

        """
        potential_files = glob.glob(os.path.join(self.plugin_dir, "*.py"))
        to_load = [(None, 'query_transform')]
        for potential_file_path in potential_files:
            dir_path = os.path.dirname(potential_file_path)
            module_name = ''.join(os.path.basename(potential_file_path).split('.')[0:-1])
            to_load.append((dir_path, module_name))
        for dir_path, module_name in to_load:
            if dir_path is not None:
                sys.path.append(dir_path)
            module = __import__(module_name)
            if dir_path is not None:
                sys.path.pop()
            for item_name in getattr(module, '__shared__', list()):
                setattr(self.match_criteria_transform.transform, item_name, getattr(module, item_name))
            for item_name in module.__export__:
                item = getattr(module, item_name)
                if issubclass(item, QueryTransformerContainer):
                    query_transform.attach_transformers_to_match_criteria_transform(self.match_criteria_transform,
                                                                                    item)

    async def _async_init(self):
        """
        Instantiate asynchronous db connections and workers.
        Create a task que which holds all matching and update tasks for processing via workers.
        """
        self._task_q = asyncio.queues.Queue()
        self._async_db_ro = MongoDBConnection(read_only=True)
        self.async_db_ro = self._async_db_ro.__enter__()
        self._async_db_rw = MongoDBConnection(read_only=False)
        self.async_db_rw = self._async_db_rw.__enter__()
        self._workers = {
            worker_id: self._loop.create_task(self._queue_worker(worker_id))
            for worker_id in range(0, self.num_workers)
        }

    async def _execute_clinical_queries(self,
                                        multi_collection_query: MultiCollectionQuery,
                                        clinical_ids: Set[ClinicalID]) -> Tuple[Set[ObjectId],
                                                                                List[ClinicalMatchReason]]:
        """
        Take in a list of queries and only execute the clinical ones. Take the resulting clinical ids, and pass that
        to the next clinical query. Repeat for all clinical queries, continuously subsetting the returned ids.
        Finally, return all clinical IDs which matched every query, and match reasons.

        Match Reasons are not used by default, but are composed of QueryNode objects and a clinical ID.
        """
        collection = self.match_criteria_transform.CLINICAL
        reasons = list()
        for query_node in multi_collection_query.clinical:
            for query_part in query_node.query_parts:
                if not query_part.render:
                    continue

                # hash the inner query to use as a reference for returned clinical ids, if necessary
                query_hash = query_part.hash()
                if query_hash not in self.cache.ids:
                    self.cache.ids[query_hash] = dict()

                # create a nested id_cache where the key is the clinical ID being queried and the vals
                # are the clinical IDs returned
                id_cache = self.cache.ids[query_hash]
                queried_ids = id_cache.keys()
                need_new = clinical_ids - set(queried_ids)

                if need_new:
                    new_query = {'$and': [{'_id': {'$in': list(need_new)}}, query_part.query]}
                    if self.debug:
                        log.info(f"{query_part.query}")
                    docs = await self.async_db_ro[collection].find(new_query, {'_id': 1}).to_list(None)

                    # save returned ids
                    for doc in docs:
                        id_cache[doc['_id']] = doc['_id']

                    # save IDs NOT returned as None so if a query is run in the future which is the same, it will skip
                    for unfound in need_new - set(id_cache.keys()):
                        id_cache[unfound] = None

                for clinical_id in list(clinical_ids):

                    # an exclusion criteria returned a clinical document hence doc is not a match
                    if id_cache[clinical_id] is not None and query_part.negate:
                        clinical_ids.remove(clinical_id)

                    # clinical doc fulfills exclusion criteria
                    elif id_cache[clinical_id] is None and query_part.negate:
                        pass

                    # doc meets inclusion criteria
                    elif id_cache[clinical_id] is not None and not query_part.negate:
                        pass

                    # no clinical doc returned for an inclusion criteria query, so remove _id from future queries
                    elif id_cache[clinical_id] is None and not query_part.negate:
                        clinical_ids.remove(clinical_id)

        for clinical_id in clinical_ids:
            for query_node in multi_collection_query.clinical:
                reasons.append(ClinicalMatchReason(query_node, clinical_id))
        return clinical_ids, reasons

    async def _execute_genomic_queries(self,
                                       multi_collection_query: MultiCollectionQuery,
                                       clinical_ids: Set[ClinicalID]) -> Tuple[Dict[ObjectId, Set[ObjectId]],
                                                                               List[GenomicMatchReason]]:
        """
        Take in a list of queries and clinical ids.
        Return an object e.g.
        { Clinical_ID : { GenomicID1, GenomicID2 etc } }
        """
        all_results: Dict[ObjectId, Set[ObjectId]] = defaultdict(set)
        potential_reasons = list()
        for genomic_query_node in multi_collection_query.genomic:
            join_field = self.match_criteria_transform.collection_mappings['genomic']['join_field']
            query = genomic_query_node.extract_raw_query()

            # Create a nested id_cache where the key is the clinical ID being queried and the vals
            # are the genomic IDs returned
            query_hash = ComparableDict(query).hash()
            if query_hash not in self.cache.ids:
                self.cache.ids[query_hash] = dict()
            id_cache = self.cache.ids[query_hash]
            queried_ids = id_cache.keys()
            need_new = clinical_ids - set(queried_ids)

            if need_new:
                new_query = query
                new_query['$and'] = new_query.setdefault('$and', list())
                new_query['$and'].insert(0, {join_field: {'$in': list(need_new)}})

                projection = {"_id": 1, join_field: 1}
                genomic_docs = await self.async_db_ro['genomic'].find(new_query, projection).to_list(None)
                if self.debug:
                    log.info(f"{new_query} returned {genomic_docs}")

                for genomic_doc in genomic_docs:
                    # If the clinical id of a returned genomic doc is not present in the cache, add it.
                    if genomic_doc[join_field] not in id_cache:
                        id_cache[genomic_doc[join_field]] = set()
                    id_cache[genomic_doc[join_field]].add(genomic_doc["_id"])

                # Clinical IDs which do not return genomic docs need to be recorded to cache exclusions
                for unfound in need_new - set(id_cache.keys()):
                    id_cache[unfound] = None

            clinical_result_ids = set()
            for clinical_id in clinical_ids:
                if id_cache[clinical_id] is not None:
                    genomic_ids = id_cache[clinical_id]
                    clinical_result_ids.add(clinical_id)

                    # Most of the time, queries associate one genomic doc to one query, but not always e.g. a patient
                    # has 2 KRAS mutations and the query is for any KRAS mutation
                    for genomic_id in genomic_ids:
                        # If an inclusion match...
                        if not genomic_query_node.exclusion:
                            all_results[clinical_id].add(genomic_id)
                            potential_reasons.append(GenomicMatchReason(genomic_query_node, clinical_id, genomic_id))

                        # If an exclusion criteria returns a genomic doc, that means that clinical ID is not a match.
                        elif genomic_query_node.exclusion and clinical_id in all_results:
                            del all_results[clinical_id]

                # If the genomic query returns nothing for an exclusion query, for a specific clinical ID, it is a match
                elif id_cache[clinical_id] is None and genomic_query_node.exclusion:
                    if clinical_id not in all_results:
                        all_results[clinical_id] = set()
                    potential_reasons.append(GenomicMatchReason(genomic_query_node, clinical_id, None))

            # If processing an inclusion query, subset existing clinical ids with clinical ids returned by the genomic
            # query. For exclusions, remove the IDs.
            if not genomic_query_node.exclusion:
                clinical_ids.intersection_update(clinical_result_ids)
            else:
                clinical_ids.difference_update(clinical_result_ids)

            if not clinical_ids:
                return dict(), list()
            else:
                # Remove everything from the output object which is not in the returned clinical IDs.
                for id_to_remove in set(all_results.keys()) - clinical_ids:
                    del all_results[id_to_remove]

        return all_results, potential_reasons

    async def _run_query(self,
                         multi_collection_query: MultiCollectionQuery,
                         initial_clinical_ids: Set[ClinicalID]) -> List[MatchReason]:
        """
        Execute a mongo query on the clinical and genomic collections to find trial matches.
        First execute the clinical query. If no records are returned short-circuit and return.
        """
        clinical_ids = set(initial_clinical_ids)
        new_clinical_ids, clinical_match_reasons = await self._execute_clinical_queries(multi_collection_query,
                                                                                        clinical_ids
                                                                                        if clinical_ids
                                                                                        else set(initial_clinical_ids))
        clinical_ids = new_clinical_ids
        if not clinical_ids:
            return list()

        all_results, genomic_match_reasons = await self._execute_genomic_queries(multi_collection_query,
                                                                                 clinical_ids
                                                                                 if clinical_ids
                                                                                 else set(initial_clinical_ids))

        needed_clinical = list()
        needed_genomic = list()
        for clinical_id, genomic_ids in all_results.items():
            if clinical_id not in self.cache.docs:
                needed_clinical.append(clinical_id)
            for genomic_id in genomic_ids:
                if genomic_id not in self.cache.docs:
                    needed_genomic.append(genomic_id)

        # matching criteria for clinical and genomic values can be set/extended in config.json
        genomic_projection = self.match_criteria_transform.genomic_projection
        clinical_projection = self.match_criteria_transform.clinical_projection
        clinical_query = MongoQuery({"_id": {"$in": list(needed_clinical)}})
        genomic_query = MongoQuery({"_id": {"$in": list(needed_genomic)}})
        results = await asyncio.gather(self._perform_db_call("clinical", clinical_query, clinical_projection),
                                       self._perform_db_call("genomic", genomic_query, genomic_projection))

        # asyncio.gather returns [[],[]]. Save the resulting values on the cache for use when creating trial matches
        for outer_result in results:
            for result in outer_result:
                self.cache.docs[result["_id"]] = result

        return [
            genomic_reason
            for genomic_reason in genomic_match_reasons
            if genomic_reason.clinical_id in all_results and any([genomic_reason.query_node.exclusion,
                                                                  genomic_reason.genomic_id in all_results[
                                                                      genomic_reason.clinical_id]])
        ]

    async def _queue_worker(self, worker_id: int) -> None:
        """
        Function which executes tasks placed on the task queue.
        """
        while True:
            # Execute update task
            task: Union[QueryTask, UpdateTask, RunLogUpdateTask, PoisonPill] = await self._task_q.get()
            if isinstance(task, PoisonPill):
                if self.debug:
                    log.info(f"Worker: {worker_id} got PoisonPill")
                self._task_q.task_done()
                break

            # Execute query task
            elif isinstance(task, QueryTask):
                if self.debug:
                    log.info(
                        f"Worker: {worker_id}, protocol_no: {task.trial['protocol_no']} got new QueryTask")
                try:
                    results = await self._run_query(task.query, task.clinical_ids)
                except Exception as e:
                    log.error(f"ERROR: Worker: {worker_id}, error: {e}")
                    results = list()
                    if isinstance(e, AutoReconnect):
                        await self._task_q.put(task)
                        self._task_q.task_done()
                    elif isinstance(e, CursorNotFound):
                        await self._task_q.put(task)
                        self._task_q.task_done()
                    else:
                        raise e
                for result in results:
                    self._queue_task_count += 1
                    if self._queue_task_count % 100 == 0:
                        log.info(f"Trial match count: {self._queue_task_count}")
                    match_document = self.create_trial_matches(TrialMatch(task.trial,
                                                                          task.match_clause_data,
                                                                          task.match_path,
                                                                          task.query,
                                                                          result))
                    self.matches[task.trial['protocol_no']][match_document['sample_id']].append(
                        match_document)
                self._task_q.task_done()

            # Execute update task
            elif isinstance(task, UpdateTask):
                try:
                    if self.debug:
                        log.info(f"Worker {worker_id} got new UpdateTask {task.protocol_no}")
                    await self.async_db_rw.trial_match_raw.bulk_write(task.ops, ordered=False)
                except Exception as e:
                    log.error(f"ERROR: Worker: {worker_id}, error: {e}")
                    if isinstance(e, AutoReconnect):
                        self._task_q.task_done()
                        await self._task_q.put(task)
                    else:
                        raise e
                finally:
                    self._task_q.task_done()

            elif isinstance(task, RunLogUpdateTask):
                try:
                    if self.debug:
                        log.info(f"Worker {worker_id} got new RunLogUpdateTask {task.run_log.protocol_no}")
                    if any([task.run_log.marked_disabled, task.run_log.marked_available, task.run_log.inserted]):
                        await self.async_db_rw.matchengine_run_log.insert_one(task.run_log.__dict__)
                except Exception as e:
                    log.error(f"ERROR: Worker: {worker_id}, error: {e}")
                    if isinstance(e, AutoReconnect):
                        self._task_q.task_done()
                        await self._task_q.put(task)
                    else:
                        raise e
                finally:
                    self._task_q.task_done()

    def extract_match_clauses_from_trial(self, protocol_no: str) -> Generator[MatchClauseData, None, None]:
        """
        Pull out all of the matches from a trial curation.
        Return the parent path and the values of that match clause.

        Default to only extracting match clauses on steps, arms or dose levels which are open to accrual unless
        otherwise specified.
        """

        trial = self.trials[protocol_no]
        trial_status = trial.setdefault('_summary', dict()).setdefault('status', [dict()])
        site_status = trial_status[0].setdefault('value', 'open to accrual').lower()
        status_for_match_clause = 'open' if site_status.lower() == 'open to accrual' else 'closed'
        coordinating_center = trial.setdefault('_summary', dict()).setdefault('coordinating_center', 'unknown')
        process_q = deque()
        for key, val in trial.items():

            # include top level match clauses
            if key == 'match':
                parent_path = ParentPath(tuple())
                yield parent_path, val
            else:
                process_q.append((tuple(), key, val))

        # process nested dicts to find more match clauses
        while process_q:
            path, parent_key, parent_value = process_q.pop()
            if isinstance(parent_value, dict):
                for inner_key, inner_value in parent_value.items():
                    if inner_key == 'match':
                        if not self.match_on_closed:
                            match_level = path[-1]
                            # suspension_key = self.match_criteria_transform.suspension_mapping.setdefault(match_level,
                            #                                                                              None)
                            if match_level == 'arm':
                                if parent_value.setdefault('arm_suspended', 'n').lower().strip() == 'y':
                                    continue
                            elif match_level == 'dose_level':
                                if parent_value.setdefault('level_suspended', 'n').lower().strip() == 'y':
                                    continue
                            elif match_level == 'step':
                                if all([arm.setdefault('arm_suspended', 'n').lower().strip() == 'y'
                                        for arm in parent_value.setdefault('arm', list())]):
                                    continue

                        parent_path = ParentPath(path + (parent_key, inner_key))
                        level = MatchClauseLevel(
                            self.match_criteria_transform.level_mapping[
                                [item for item in parent_path[::-1] if not isinstance(item, int) and item != 'match'][
                                    0]])

                        internal_id = parent_value[self.match_criteria_transform.internal_id_mapping[level]]
                        code = parent_value[self.match_criteria_transform.code_mapping[level]]
                        yield MatchClauseData(inner_value,
                                              internal_id,
                                              code,
                                              coordinating_center,
                                              status_for_match_clause,
                                              parent_path,
                                              level,
                                              parent_value,
                                              trial['protocol_no'])
                    else:
                        process_q.append((path + (parent_key,), inner_key, inner_value))
            elif isinstance(parent_value, list):
                for index, item in enumerate(parent_value):
                    process_q.append((path + (parent_key,), index, item))

    def create_match_tree(self, match_clause_data: MatchClauseData) -> MatchTree:
        """
        Turn a match clause from a trial curation into a digraph
        """
        match_clause = match_clause_data.match_clause
        process_q: deque[Tuple[NodeID, Dict[str, Any]]] = deque()
        graph = nx.DiGraph()
        node_id: NodeID = NodeID(1)
        graph.add_node(0)  # root node is 0
        graph.nodes[0]['criteria_list'] = list()
        graph.nodes[0]['is_and'] = True
        graph.nodes[0]['or_nodes'] = set()
        graph.nodes[0]['label'] = '0 - ROOT and'
        graph.nodes[0]['label_list'] = list()
        for item in match_clause:
            if any([k.startswith('or') for k in item.keys()]):
                process_q.appendleft((NodeID(0), item))
            else:
                process_q.append((NodeID(0), item))

        def graph_match_clause():
            """

            """
            import matplotlib.pyplot as plt
            from networkx.drawing.nx_agraph import graphviz_layout
            import os
            labels = {node: graph.nodes[node]['label'] for node in graph.nodes}
            for node in graph.nodes:
                if graph.nodes[node]['label_list']:
                    labels[node] = labels[node] + ' [' + ','.join(graph.nodes[node]['label_list']) + ']'
            pos = graphviz_layout(graph, prog="dot", root=0)
            plt.figure(figsize=(30, 30))
            nx.draw_networkx(graph, pos, with_labels=True, node_size=[600 for _ in graph.nodes], labels=labels)
            plt.savefig(os.path.join(self.fig_dir, (f'{match_clause_data.protocol_no}-'
                                                    f'{match_clause_data.match_clause_level}-'
                                                    f'{match_clause_data.internal_id}.png')))
            return plt

        while process_q:
            parent_id, values = process_q.pop()
            # parent_is_or = True if graph.nodes[parent_id].setdefault('is_or', False) else False
            parent_is_and = True if graph.nodes[parent_id].setdefault('is_and', False) else False
            for label, value in values.items():  # label is 'and', 'or', 'genomic' or 'clinical'
                if label.startswith('and'):
                    criteria_list = list()
                    label_list = list()
                    for item in value:
                        for inner_label, inner_value in item.items():
                            if inner_label.startswith("or"):
                                process_q.appendleft((parent_id if parent_is_and else node_id, item))
                            elif inner_label.startswith("and"):
                                process_q.append((parent_id if parent_is_and else node_id, item))
                            else:
                                criteria_list.append(item)
                                label_list.append(inner_label)
                    if parent_is_and:
                        graph.nodes[parent_id]['criteria_list'].extend(criteria_list)
                        graph.nodes[parent_id]['label_list'].extend(label_list)
                    else:
                        graph.add_edges_from([(parent_id, node_id)])
                        graph.nodes[node_id].update({
                            'criteria_list': criteria_list,
                            'is_and': True,
                            'is_or': False,
                            'or_nodes': set(),
                            'label': str(node_id) + ' - ' + label,
                            'label_list': label_list
                        })
                        node_id += 1
                elif label.startswith("or"):
                    or_node_id = node_id
                    graph.add_node(or_node_id)
                    graph.nodes[or_node_id].update({
                        'criteria_list': list(),
                        'is_and': False,
                        'is_or': True,
                        'label': str(or_node_id) + ' - ' + label,
                        'label_list': list()
                    })
                    node_id += 1
                    for item in value:
                        process_q.append((or_node_id, item))
                    if parent_is_and:
                        parent_or_nodes = graph.nodes[parent_id]['or_nodes']
                        if not parent_or_nodes:
                            graph.add_edges_from([(parent_id, or_node_id)])
                            graph.nodes[parent_id]['or_nodes'] = {or_node_id}
                        else:
                            successors = [
                                (successor, or_node_id)
                                for parent_or_node in parent_or_nodes
                                for successor in nx.descendants(graph, parent_or_node)
                                if graph.out_degree(successor) == 0
                            ]
                            graph.add_edges_from(successors)
                    else:
                        graph.add_edge(parent_id, or_node_id)
                else:
                    if parent_is_and:
                        graph.nodes[parent_id]['criteria_list'].append(values)
                        graph.nodes[parent_id]['label_list'].append(label)
                    else:
                        graph.add_node(node_id)
                        graph.nodes[node_id].update({
                            'criteria_list': [values],
                            'is_or': False,
                            'is_and': True,
                            'label': str(node_id) + ' - ' + label,
                            'label_list': list()
                        })
                        graph.add_edge(parent_id, node_id)
                        node_id += 1

        if self.visualize_match_paths:
            graph_match_clause()
        return MatchTree(graph)

    @staticmethod
    def get_match_paths(match_tree: MatchTree) -> Generator[MatchCriterion, None, None]:
        """
        Takes a MatchTree (from create_match_tree) and yields the criteria from each possible path on the tree,
        from the root node to each leaf node
        """
        leaves = list()
        for node in match_tree.nodes:
            if match_tree.out_degree(node) == 0:
                leaves.append(node)
        for leaf in leaves:
            for path in nx.all_simple_paths(match_tree, 0, leaf) if leaf != 0 else [[leaf]]:
                match_path = MatchCriterion(list())
                for node in path:
                    if match_tree.nodes[node]['criteria_list']:
                        match_path.append(match_tree.nodes[node]['criteria_list'])
                if match_path:
                    yield match_path

    def translate_match_path(self,
                             match_clause_data: MatchClauseData,
                             match_criterion: MatchCriterion) -> MultiCollectionQuery:
        """
        Translate the keys/values from the trial curation into keys/values used in a genomic/clinical document.
        Uses an external config file ./config/config.json

        """
        multi_collection_query = MultiCollectionQuery(list(), list())
        query_cache = set()
        for node in match_criterion:
            for criteria in node:
                for genomic_or_clinical, values in criteria.items():
                    query_node = QueryNode(genomic_or_clinical, list(), None)
                    for trial_key, trial_value in values.items():
                        trial_key_settings = self.match_criteria_transform.trial_key_mappings[
                            genomic_or_clinical].setdefault(
                            trial_key.upper(),
                            dict())

                        if trial_key_settings.setdefault('ignore', False):
                            continue

                        sample_value_function_name = trial_key_settings.setdefault('sample_value', 'nomap')
                        sample_function = getattr(self.match_criteria_transform.query_transformers,
                                                  sample_value_function_name)
                        sample_function_args = dict(sample_key=trial_key.upper(),
                                                    trial_value=trial_value,
                                                    parent_path=match_clause_data.parent_path,
                                                    trial_path=genomic_or_clinical,
                                                    trial_key=trial_key,
                                                    query_node=query_node)
                        sample_function_args.update(trial_key_settings)
                        sample_value, negate = sample_function(**sample_function_args)
                        query_part = QueryPart(sample_value, negate, True)
                        query_node.query_parts.append(query_part)
                        # set the exclusion = True on the query node if ANY of the query parts are negate
                        query_node.exclusion = True if negate or query_node.exclusion else False
                    if query_node.exclusion is not None:
                        query_node_transform(query_node)
                        query_node_hash = query_node.hash()
                        if query_node_hash not in query_cache:
                            getattr(multi_collection_query, genomic_or_clinical).append(query_node)
                            query_cache.add(query_node_hash)
        return multi_collection_query

    def update_matches_for_protocol_number(self, protocol_no):
        """
        Updates all trial matches for a given protocol number
        """
        self._loop.run_until_complete(self._async_update_matches_by_protocol_no(protocol_no))

    def update_all_matches(self):
        """
        Synchronoususly iterates over each protocol number, updating the matches in the database for each
        """
        for protocol_number in self.protocol_nos:
            self.update_matches_for_protocol_number(protocol_number)

    async def _async_update_matches_by_protocol_no(self, protocol_no: str):
        """
        Update trial matches by diff'ing the newly created trial matches against existing matches in the db.
        'Delete' matches by adding {is_disabled: true} and insert all new matches.
        """
        trial_matches_by_sample_id = self.matches.setdefault(protocol_no, dict())
        log.info(f"Updating trial matches for {protocol_no}")
        remaining_to_disable = [
            result
            for result in await self._perform_db_call(collection='trial_match_raw',
                                                      query=MongoQuery(
                                                          {
                                                              'protocol_no': protocol_no,
                                                              "sample_id": {
                                                                  '$nin': list(trial_matches_by_sample_id.keys())
                                                              }
                                                          }),
                                                      projection={'_id': 1, 'hash': 1, 'clinical_id': 1})
        ]
        initial_delete_ops = [
            UpdateMany(filter={'hash': {'$in': [result['hash']
                                                for result in remaining_to_disable]}},
                       update={'$set': {"is_disabled": True}})
        ]
        await self._task_q.put(UpdateTask(initial_delete_ops, protocol_no))
        deleted_by_id: Dict[RunLog] = dict()
        for to_disable in remaining_to_disable:
            clinical_id = to_disable['clinical_id']
            if clinical_id not in deleted_by_id:
                deleted_by_id[clinical_id] = RunLog(protocol_no, clinical_id)
            run_log = deleted_by_id[clinical_id]
            run_log.marked_disabled.append(to_disable['hash'])

        for run_log in deleted_by_id.values():
            await self._task_q.put(RunLogUpdateTask(run_log))
        await self._task_q.put(UpdateTask(initial_delete_ops, protocol_no))
        for sample_id in trial_matches_by_sample_id.keys():
            run_log = RunLog(protocol_no, self.sample_mapping[sample_id])
            new_matches_hashes = [match['hash'] for match in trial_matches_by_sample_id[sample_id]]

            trial_matches_to_not_change_query = MongoQuery({'hash': {'$in': new_matches_hashes}})
            trial_matches_to_disable_query = MongoQuery({'protocol_no': protocol_no,
                                                         'sample_id': sample_id,
                                                         'is_disabled': False,
                                                         'hash': {'$nin': new_matches_hashes}})
            projection = {"hash": 1, "is_disabled": 1}
            trial_matches_existent_results, trial_matches_to_disable = await asyncio.gather(
                self._perform_db_call('trial_match_raw', trial_matches_to_not_change_query, projection),
                self._perform_db_call('trial_match_raw', trial_matches_to_disable_query, projection)
            )

            trial_matches_hashes_existent = {
                result['hash']
                for result
                in trial_matches_existent_results
            }
            trial_matches_disabled = {
                result['hash']
                for result in trial_matches_existent_results
                if result['is_disabled']
            }

            trial_matches_to_insert = [
                trial_match
                for trial_match in trial_matches_by_sample_id[sample_id]
                if trial_match['hash'] not in trial_matches_hashes_existent
            ]
            trial_matches_to_mark_available = [
                trial_match
                for trial_match in trial_matches_by_sample_id[sample_id]
                if trial_match['hash'] in trial_matches_disabled
            ]

            run_log.inserted.extend([
                trial_match['hash']
                for trial_match in trial_matches_to_insert
            ])
            run_log.marked_available.extend([
                trial_match['hash']
                for trial_match in trial_matches_to_mark_available
            ])
            run_log.marked_disabled.extend([
                trial_match['hash']
                for trial_match in trial_matches_to_disable
            ])

            ops = list()
            ops.append(UpdateMany(filter={'hash': {'$in': [trial_match['hash']
                                                           for trial_match in trial_matches_to_disable]}},
                                  update={'$set': {'is_disabled': True}}))
            for to_insert in trial_matches_to_insert:
                ops.append(InsertOne(document=to_insert))
            ops.append(UpdateMany(filter={'hash': {'$in': [trial_match['hash']
                                                           for trial_match in trial_matches_to_mark_available]}},
                                  update={'$set': {'is_disabled': False}}))
            await self._task_q.put(RunLogUpdateTask(run_log))

            await self._task_q.put(UpdateTask(ops, protocol_no))

        await self._task_q.join()

    def get_matches_for_all_trials(self) -> Dict[str, Dict[str, List]]:
        """
        Synchronously iterates over each protocol number, getting trial matches for each
        """
        for protocol_no in self.protocol_nos:
            self.get_matches_for_trial(protocol_no)
        return self.matches

    def get_matches_for_trial(self, protocol_no: str):
        """
        Get the trial matches for a given protocol number
        """
        log.info(f"Begin Protocol No: {protocol_no}")
        task = self._loop.create_task(self._async_get_matches_for_trial(protocol_no))
        return self._loop.run_until_complete(task)

    async def _async_get_matches_for_trial(self, protocol_no: str) -> Dict[str, List[Dict]]:
        """
        Asynchronous function used by get_matches_for_trial, not meant to be called externally.
        Gets the matches for a given trial
        """
        trial = self.trials[protocol_no]
        if not self.match_on_deceased and trial['status'].lower().strip() not in {"open to accrual"}:
            logging.info('Trial %s is closed, skipping' % trial['protocol_no'])
            return dict()
        else:
            # Get each match clause in the trial document
            match_clauses = self.extract_match_clauses_from_trial(protocol_no)

            # for each match clause, create the match tree, and extract each possible match path from the tree
            for match_clause in match_clauses:
                match_paths = self.get_match_paths(self.create_match_tree(match_clause))

                # for each match path, translate the path into valid mongo queries
                for match_path in match_paths:
                    query = self.translate_match_path(match_clause, match_path)
                    if self.debug:
                        log.info(f"Query: {query}")
                    if query:
                        # put the query onto the task queue for execution
                        await self._task_q.put(QueryTask(trial,
                                                         match_clause,
                                                         match_path,
                                                         query,
                                                         self.clinical_ids))
            await self._task_q.join()
            logging.info(f"Total results: {len(self.matches[protocol_no])}")
            return self.matches[protocol_no]

    def get_clinical_ids_from_sample_ids(self) -> Dict[ClinicalID, str]:
        """

        """
        # if no sample ids are passed in as args, get all clinical documents
        query: Dict = {} if self.match_on_deceased else {"VITAL_STATUS": 'alive'}
        if self.sample_ids is not None:
            query.update({"SAMPLE_ID": {"$in": self.sample_ids}})
        return {result['_id']: result['SAMPLE_ID']
                for result in self.db_ro.clinical.find(query, {'_id': 1, 'SAMPLE_ID': 1})}

    def get_trials(self) -> Dict[str, Trial]:
        """
        Gets all the trial documents in the database, or just the relevant trials (if protocol numbers supplied)
        """
        trial_find_query = dict()

        # matching criteria can be set and extended in config.json. for more details see the README
        projection = self.match_criteria_transform.trial_projection

        if self.protocol_nos is not None:
            trial_find_query['protocol_no'] = {"$in": [protocol_no for protocol_no in self.protocol_nos]}

        all_trials = {
            result['protocol_no']: result
            for result in self.db_ro.trial.find(trial_find_query, projection)
        }
        if self.match_on_closed:
            return all_trials
        else:
            open_trials = dict()
            for protocol_no, trial in all_trials.items():
                open_trials.update({protocol_no: trial})
            return open_trials

    async def _perform_db_call(self, collection: str, query: MongoQuery, projection: Dict):
        """
        Asynchronously executes a find query on the database, with specified query and projection and a collection
        Used to parallelize DB calls, with asyncio.gather
        """
        return await self.async_db_ro[collection].find(query, projection).to_list(None)

    def get_sort_order(self, match_document: Dict) -> str:
        """
        Sort trial matches based on sorting order specified in config.json under the key 'trial_match_sorting'.

        The function will iterate over the objects in the 'trial_match_sorting', and then assess each trial match key
        to determine a final sort string e.g. 001010111000

        The sorting is multi-dimensional and currently organized as follows:
        MMR status > Tier 1 > Tier 2 > CNV > Tier 3 > Tier 4 > wild type
        Variant-level  > gene-level
        Exact cancer match > all solid/liquid
        DFCI > Coordinating centers
        """
        sub_level_padding = 2
        sub_level_slots = 4
        top_level_slots = 5
        sort_order_mapping = self.config['trial_match_sorting']
        top_level_sort = str()
        for top_level_position, top_level_sort_mapping in enumerate(sort_order_mapping):
            if top_level_position >= top_level_slots:
                break
            sub_level_sort = str()
            for sub_level_position, sub_level_sort_mapping in enumerate(top_level_sort_mapping):
                if sub_level_position >= sub_level_slots:
                    break
                sort_key, sort_values = sub_level_sort_mapping
                if match_document.setdefault(sort_key, None) in sort_values:
                    sub_level_sort += str(sort_values.index(match_document[sort_key])).ljust(sub_level_padding, '0')
                else:
                    sub_level_sort += str((10 ** sub_level_padding) - 1).ljust(sub_level_padding, '0')
            top_level_sort += sub_level_sort
        return top_level_sort
        # return ''.join([top_level_sort[slot] for slot in range(0, top_level_slots)])

    def create_trial_matches(self, trial_match: TrialMatch) -> Dict:
        """
        Create a trial match document to be inserted into the db. Add clinical, genomic, and trial details as specified
        in config.json
        """
        genomic_doc = self.cache.docs.setdefault(trial_match.match_reason.genomic_id, None)
        query = trial_match.match_reason.query_node.extract_raw_query()

        new_trial_match = dict()
        new_trial_match.update(format_trial_match_k_v(self.cache.docs[trial_match.match_reason.clinical_id]))
        new_trial_match['clinical_id'] = self.cache.docs[trial_match.match_reason.clinical_id]

        if genomic_doc is None:
            new_trial_match.update(format_trial_match_k_v(format_exclusion_match(query)))
        else:
            new_trial_match.update(format_trial_match_k_v(get_genomic_details(genomic_doc, query)))

        new_trial_match.update(
            {'match_level': trial_match.match_clause_data.match_clause_level,
             'internal_id': trial_match.match_clause_data.internal_id,
             'code': trial_match.match_clause_data.code,
             'trial_accrual_status': trial_match.match_clause_data.status,
             'coordinating_center': trial_match.match_clause_data.coordinating_center})

        # remove extra fields from trial_match output
        new_trial_match.update({
            k: v
            for k, v in trial_match.trial.items()
            if k not in {'treatment_list', '_summary', 'status', '_id', '_elasticsearch', 'match'}
        })
        sort_order = self.get_sort_order(new_trial_match)
        new_trial_match['sort_order_raw'] = sort_order
        new_trial_match['query_hash'] = ComparableDict({'query': trial_match.match_criterion}).hash()
        new_trial_match['hash'] = ComparableDict(new_trial_match).hash()
        new_trial_match["is_disabled"] = False
        new_trial_match.update(
            {'match_path': '.'.join([str(item) for item in trial_match.match_clause_data.parent_path])})
        return new_trial_match


def main(run_args):
    """

    """
    check_indices()
    with MatchEngine(plugin_dir=run_args.plugin_dir, sample_ids=run_args.samples, protocol_nos=run_args.trials,
                     match_on_closed=run_args.match_on_closed, match_on_deceased=run_args.match_on_deceased,
                     debug=run_args.debug, num_workers=run_args.workers[0], config_path=args.config_path) as me:
        me.get_matches_for_all_trials()
        if not args.dry:
            me.update_all_matches()


if __name__ == "__main__":
    # todo unit tests
    # todo output CSV file functions
    # todo update/delete/insert run log
    # todo failsafes for insert logic (fallback?)
    # todo increase db cursor timeout
    # todo db connection timeout
    # todo trial_match view (for sort_order)
    # todo configuration of trial_match document logic

    param_trials_help = 'Path to your trial data file or a directory containing a file for each trial.' \
                        'Default expected format is YML.'
    param_mongo_uri_help = 'Your MongoDB URI. If you do not supply one, for matching, it will default to whatever' \
                           ' is set to "MONGO_URI" in your SECRETS.JSON file. This file must be set as an ' \
                           'environmental variable. For data loading you must specify a URI with a database ' \
                           'ex: mongodb://localhost:27017/matchminer. ' \
                           'See https://docs.mongodb.com/manual/reference/connection-string/ for more information.'
    param_clinical_help = 'Path to your clinical file. Default expected format is CSV.'
    param_genomic_help = 'Path to your genomic file. Default expected format is CSV'
    param_outpath_help = 'Destination and name of your results file.'
    param_trial_format_help = 'File format of input trial data. Default is YML.'
    param_patient_format_help = 'File format of input patient data (both clinical and genomic files). Default is CSV.'

    parser = argparse.ArgumentParser()
    closed_help = 'Match on all closed trials and all suspended steps, arms and doses. Default is to skip.'
    deceased_help = 'Match on deceased patients. Default is to match only on alive patients.'
    upsert_help = 'When loading clinical or trial data, specify a field other than _id to use as a unique key. ' \
                  'Must be comma separated values if using more than one field e.g. name,age,gender'
    dry_help = "Execute a full matching run but do not insert any matches into the DB"
    debug_help = "Enable debug logging"
    config_help = "Path to the config file. By default will look in config/config.json, but if this class is " \
                  "imported, will need to be specified explicitly "

    subp = parser.add_subparsers(help='sub-command help')
    subp_p = subp.add_parser('load', help='Sets up your MongoDB for matching.')
    subp_p.add_argument('-t', dest='trials', help=param_trials_help)
    subp_p.add_argument('-c', dest='clinical', help=param_clinical_help)
    subp_p.add_argument('-g', dest='genomic', help=param_genomic_help)
    subp_p.add_argument('--mongo-uri', dest='mongo_uri', required=True, default=None, help=param_mongo_uri_help)
    subp_p.add_argument('--trial-format',
                        dest='trial_format',
                        default='yml',
                        action='store',
                        choices=['yml', 'json', 'bson'],
                        help=param_trial_format_help)
    subp_p.add_argument('--patient-format', dest='patient_format',
                        default='csv',
                        action='store',
                        choices=['csv', 'pkl', 'bson', 'json'],
                        help=param_patient_format_help)
    subp_p.add_argument('--upsert-fields', dest='upsert_fields', default='', required=False, help=upsert_help)
    subp_p.set_defaults(func=load)

    subp_p = subp.add_parser('match', help='Match patients to trials.')
    subp_p.add_argument("-trials", nargs="*", type=str, default=None)
    subp_p.add_argument("-samples", nargs="*", type=str, default=None)
    subp_p.add_argument("--match-on-closed",
                        dest="match_on_closed",
                        action="store_true",
                        default=False,
                        help=closed_help)
    subp_p.add_argument("--visualize-match-paths",
                        dest="visualize_match_paths",
                        action="store_true",
                        default=False,
                        help="Enable to render images of all match paths")
    subp_p.add_argument("--fig-dir",
                        dest="fig_dir",
                        default='img',
                        help="Directory to store match path images")
    subp_p.add_argument("--dry-run", dest="dry", action="store_true", default=False, help=dry_help)
    subp_p.add_argument("--debug", dest="debug", action="store_true", default=False, help=debug_help)
    subp_p.add_argument("--config-path", dest="config_path", default="config/config.json", help=config_help)
    subp_p.add_argument("--override-plugin-dir", dest="plugin_dir", default="plugins", help=config_help)
    subp_p.add_argument("--match-on-deceased-patients",
                        dest="match_on_deceased",
                        action="store_true",
                        help=deceased_help)
    subp_p.add_argument("-workers", nargs=1, type=int, default=[cpu_count() * 5])
    subp_p.add_argument('-o', dest="outpath", required=False, help=param_outpath_help)
    args = parser.parse_args()
    # args.func(args)
    main(args)

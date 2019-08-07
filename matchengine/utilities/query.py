from __future__ import annotations

import asyncio
import logging
import operator
from functools import reduce
from typing import TYPE_CHECKING

from matchengine.typing.matchengine_types import (
    ClinicalMatchReason,
    GenomicMatchReason,
    MongoQuery
)
from matchengine.utilities.utilities import perform_db_call

if TYPE_CHECKING:
    from bson import ObjectId
    from matchengine.engine import MatchEngine
    from matchengine.typing.matchengine_types import (
        ClinicalID,
        MultiCollectionQuery
    )
    from typing import (
        Tuple,
        Set,
        List,
    )

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('matchengine')


async def execute_clinical_queries(matchengine: MatchEngine,
                                   multi_collection_query: MultiCollectionQuery,
                                   clinical_ids: Set[ClinicalID]) -> Tuple[Set[ObjectId],
                                                                           List[ClinicalMatchReason]]:
    """
    Take in a list of queries and only execute the clinical ones. Take the resulting clinical ids, and pass that
    to the next clinical query. Repeat for all clinical queries, continuously subsetting the returned ids.
    Finally, return all clinical IDs which matched every query, and match reasons.

    Match Reasons are not used by default, but are composed of QueryNode objects and a clinical ID.
    """
    collection = matchengine.match_criteria_transform.CLINICAL
    reasons = list()
    reasons_cache = set()
    query_nodes_by_hash = dict()
    for _clinical in multi_collection_query.clinical:
        for query_node in _clinical.query_nodes:
            clinical_ids = matchengine.clinical_query_node_clinical_ids_subsetter(query_node, clinical_ids)
            query_nodes_by_hash[query_node.hash()] = query_node
            for query_part in query_node.query_parts:
                if not query_part.render:
                    continue

                # hash the inner query to use as a reference for returned clinical ids, if necessary
                query_hash = query_part.hash()
                if query_hash not in matchengine.cache.ids:
                    matchengine.cache.ids[query_hash] = dict()

                # create a nested id_cache where the key is the clinical ID being queried and the vals
                # are the clinical IDs returned
                id_cache = matchengine.cache.ids[query_hash]
                queried_ids = set(id_cache.keys())
                waiting_for = matchengine.cache.in_process.setdefault(query_hash, set())
                need_new = clinical_ids - queried_ids - waiting_for
                matchengine.cache.in_process.setdefault(query_hash, set()).update(need_new)

                if need_new:
                    new_query = {'$and': [{'_id': {'$in': list(need_new)}}, query_part.query]}
                    if matchengine.debug:
                        log.info(f"{query_part.query}")
                    docs = await matchengine.async_db_ro[collection].find(new_query, {'_id': 1}).to_list(None)

                    # save returned ids
                    for doc in docs:
                        id_cache[doc['_id']] = doc['_id']

                    # save IDs NOT returned as None so if a query is run in the future which is the same, it will skip
                    for unfound in need_new - set(id_cache.keys()):
                        id_cache[unfound] = None
                    matchengine.cache.in_process[query_hash] -= need_new

                while matchengine.cache.in_process[query_hash] & waiting_for:
                    await asyncio.sleep(1)
                for clinical_id in list(clinical_ids):

                    # an exclusion criteria returned a clinical document hence doc is not a match
                    if id_cache[clinical_id] is not None and query_part.negate:
                        clinical_ids.remove(clinical_id)
                        if (query_node.hash(), clinical_id) in reasons_cache:
                            reasons_cache.remove((query_node.hash(), clinical_id))

                    # clinical doc fulfills exclusion criteria
                    elif id_cache[clinical_id] is None and query_part.negate:
                        reasons_cache.add((query_node.hash(), clinical_id))
                        pass

                    # doc meets inclusion criteria
                    elif id_cache[clinical_id] is not None and not query_part.negate:
                        reasons_cache.add((query_node.hash(), clinical_id))
                        pass

                    # no clinical doc returned for an inclusion criteria query, so remove _id from future queries
                    elif id_cache[clinical_id] is None and not query_part.negate:
                        clinical_ids.remove(clinical_id)
                        if (query_node.hash(), clinical_id) in reasons_cache:
                            reasons_cache.remove((query_node.hash(), clinical_id))

    for query_node_hash, clinical_id in reasons_cache:
        reasons.append(ClinicalMatchReason(query_nodes_by_hash[query_node_hash], clinical_id))
    return clinical_ids, reasons


async def execute_genomic_queries(matchengine: MatchEngine,
                                  multi_collection_query: MultiCollectionQuery,
                                  initial_clinical_ids: Set[ClinicalID]) -> Tuple[Set[ObjectId], Set[ObjectId],
                                                                                  List[GenomicMatchReason]]:
    clinical_ids = {clinical_id: set() for clinical_id in initial_clinical_ids}
    qnc_qn_tracker = dict()
    width_cache = dict()
    join_field = matchengine.match_criteria_transform.collection_mappings['genomic']['join_field']
    for qnc_idx, genomic_query_node_container in enumerate(multi_collection_query.genomic):
        query_node_container_clinical_ids = list()
        for qn_idx, genomic_query_node in enumerate(genomic_query_node_container.query_nodes):
            query_node_container_clinical_ids.append(
                matchengine.genomic_query_node_clinical_ids_subsetter(genomic_query_node, clinical_ids.keys())
            )
            working_clinical_ids = query_node_container_clinical_ids[qn_idx]

            # Create a nested id_cache where the key is the clinical ID being queried and the vals
            # are the genomic IDs returned
            query_hash = genomic_query_node.raw_query_hash()
            if query_hash not in matchengine.cache.ids:
                matchengine.cache.ids[query_hash] = dict()
            id_cache = matchengine.cache.ids[query_hash]
            queried_ids = set(id_cache.keys())
            waiting_for = matchengine.cache.in_process.setdefault(query_hash, set())
            need_new = working_clinical_ids - queried_ids - waiting_for
            matchengine.cache.in_process.setdefault(query_hash, set()).update(need_new)
            query = genomic_query_node.extract_raw_query()

            if need_new:
                new_query = query
                new_query['$and'] = new_query.get('$and', list())
                new_query['$and'].insert(0, {join_field: {'$in': list(need_new)}})

                projection = {"_id": 1, join_field: 1}
                genomic_docs = await matchengine.async_db_ro['genomic'].find(new_query, projection).to_list(None)
                if matchengine.debug:
                    log.info(f"{new_query} returned {genomic_docs}")

                for genomic_doc in genomic_docs:
                    # If the clinical id of a returned genomic doc is not present in the cache, add it.
                    if genomic_doc[join_field] not in id_cache:
                        id_cache[genomic_doc[join_field]] = set()
                    id_cache[genomic_doc[join_field]].add(genomic_doc["_id"])

                # Clinical IDs which do not return genomic docs need to be recorded to cache exclusions
                for unfound in need_new - set(id_cache.keys()):
                    id_cache[unfound] = None
                matchengine.cache.in_process[query_hash] -= need_new
            while matchengine.cache.in_process[query_hash] & waiting_for:
                await asyncio.sleep(1)
            returned_clinical_ids = {clinical_id
                                     for clinical_id, genomic_docs
                                     in id_cache.items()
                                     if genomic_docs is not None}
            not_returned_clinical_ids = working_clinical_ids - returned_clinical_ids
            clinical_width = (len(not_returned_clinical_ids)
                              if genomic_query_node.exclusion
                              else len(returned_clinical_ids))
            genomic_width = (1
                             if genomic_query_node.exclusion
                             else sum(map(len, filter(bool, id_cache.values()))))
            width_cache[(qnc_idx, qn_idx)] = (clinical_width, genomic_width)
            working_clinical_ids &= not_returned_clinical_ids if genomic_query_node.exclusion else returned_clinical_ids
        current_clinical_ids = set(clinical_ids.keys())
        qnc_clinical_ids = {
            clinical_id
            for clinical_id
            in reduce(operator.or_, query_node_container_clinical_ids, set())
        }
        for invalid_clinical in current_clinical_ids - qnc_clinical_ids:
            all_qnc_qn_to_remove = clinical_ids.pop(invalid_clinical)
            for qnc_qn_to_remove in all_qnc_qn_to_remove:
                qnc_qn_tracker[qnc_qn_to_remove].remove(invalid_clinical)
        for qn_idx, qn_results in enumerate(query_node_container_clinical_ids):
            for valid_clinical_id in qn_results & qnc_clinical_ids:
                clinical_ids[valid_clinical_id].add((qnc_idx, qn_idx))
            qnc_qn_tracker[(qnc_idx, qn_idx)] = qn_results

    reasons = list()
    all_genomic = set()
    for (qnc_idx, qn_idx), found_clinical_ids in qnc_qn_tracker.items():
        genomic_query_node_container = multi_collection_query.genomic[qnc_idx]
        query_node = genomic_query_node_container.query_nodes[qn_idx]
        clinical_width, genomic_width = width_cache[(qnc_idx, qn_idx)]
        for clinical_id in found_clinical_ids:
            genomic_ids = matchengine.cache.ids[query_node.raw_query_hash()][clinical_id]
            if genomic_ids is not None:
                all_genomic.update(genomic_ids)
            for genomic_id in (genomic_ids if genomic_ids is not None else [None]):
                reasons.append(GenomicMatchReason(query_node, genomic_width, clinical_width, clinical_id, genomic_id))

    return set(clinical_ids.keys()), all_genomic, reasons


async def get_docs_results(matchengine: MatchEngine, needed_clinical, needed_genomic):
    """
    Matching criteria for clinical and genomic values can be set/extended in config.json
    :param matchengine:
    :param needed_clinical:
    :param needed_genomic:
    :return:
    """
    genomic_projection = matchengine.match_criteria_transform.genomic_projection
    clinical_projection = matchengine.match_criteria_transform.clinical_projection
    clinical_query = MongoQuery({"_id": {"$in": list(needed_clinical)}})
    genomic_query = MongoQuery({"_id": {"$in": list(needed_genomic)}})
    results = await asyncio.gather(perform_db_call(matchengine, "clinical", clinical_query, clinical_projection),
                                   perform_db_call(matchengine, "genomic", genomic_query, genomic_projection))
    return results


def get_valid_genomic_reasons(genomic_match_reasons, clinical_ids, genomic_ids):
    return [
        genomic_reason
        for genomic_reason in genomic_match_reasons
        if genomic_reason.clinical_id in clinical_ids and any([genomic_reason.query_node.exclusion,
                                                               genomic_reason.genomic_id in genomic_ids])
    ]


def get_valid_clinical_reasons(clinical_match_reasons, clinical_ids):
    return [
        clinical_reason
        for clinical_reason in clinical_match_reasons
        if clinical_reason.clinical_id in clinical_ids
    ]

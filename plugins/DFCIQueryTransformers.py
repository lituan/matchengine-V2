import datetime
import re

from dateutil.relativedelta import relativedelta

from query_transform import QueryTransformerContainer


class DFCITransformers(QueryTransformerContainer):
    def age_range_to_date_query(self, **kwargs):
        sample_key = kwargs['sample_key']
        trial_value = kwargs['trial_value']
        operator_map = {
            "==": "$eq",
            "<=": "$gte",
            ">=": "$lte",
            ">": "$lt",
            "<": "$gt"
        }
        # funky logic is because 1 month curation is curated as "0.083" (1/12 a year)
        operator = ''.join([i for i in trial_value if not i.isdigit() and i != '.'])
        numeric = "".join([i for i in trial_value if i.isdigit() or i == '.'])
        split_time = numeric.split('.')
        years = int(split_time[0] if split_time[0].isdigit() else 0)
        months_fraction = float(split_time[1]) if len(split_time) > 1 else 0
        months = int(months_fraction * 12)
        current_date = datetime.date.today()
        query_date = current_date - relativedelta(years=years, months=months)
        query_datetime = datetime.datetime(query_date.year, query_date.month, query_date.day, 0, 0, 0, 0)
        return {sample_key: {operator_map[operator]: query_datetime}}, False

    def bool_from_text(self, **kwargs):
        trial_value = kwargs['trial_value']
        sample_key = kwargs['sample_key']
        if trial_value.upper() == 'TRUE':
            return {sample_key: True}, False
        elif trial_value.upper() == 'FALSE':
            return {sample_key: False}, False

    def cnv_map(self, **kwargs):
        # Heterozygous deletion,
        # Gain,
        # Homozygous deletion,
        # High level amplification,
        # Neu

        trial_value = kwargs['trial_value']
        sample_key = kwargs['sample_key']
        cnv_map = {
            "High Amplification": "High level amplification",
            "Homozygous Deletion": "Homozygous deletion",
            'Low Amplification': 'Gain',
            'Heterozygous Deletion': 'Heterozygous deletion'

        }

        trial_value, negate = self._.transform.is_negate(trial_value)
        if trial_value in cnv_map:
            return {sample_key: cnv_map[trial_value]}, negate
        else:
            return {sample_key: trial_value}, negate

    def variant_category_map(self, **kwargs):
        trial_value = kwargs['trial_value']
        sample_key = kwargs['sample_key']
        variant_category_map = {
            "Copy Number Variation": "CNV",
            "Any Variation": {"$in": ["MUTATION", "CNV"]}
        }

        trial_value, negate = self._.transform.is_negate(trial_value)

        # if a curation calls for a Structural Variant, search the free text in the genomic document under
        # STRUCTURAL_VARIANT_COMMENT for mention of the TRUE_HUGO_SYMBOL
        if trial_value == 'Structural Variation':
            return {'STRUCTURAL_VARIANT_COMMENT': None}, negate
        elif trial_value in variant_category_map:
            return {sample_key: variant_category_map[trial_value]}, negate
        else:
            return {sample_key: trial_value.upper()}, negate

    def wildcard_regex(self, **kwargs):
        """
        When trial curation criteria include a wildcard prefix (e.g. WILDCARD_PROTEIN_CHANGE), a genomic query must
        use a $regex to search for all genomic documents which match the protein prefix.

        E.g.
        Trial curation match clause:
        | genomic:
        |    wildcard_protein_change: p.R132

        Patient genomic data:
        |    true_protein_change: p.R132H

        The above should match in a mongo query.
        """
        trial_value = kwargs['trial_value']

        # By convention, all protein changes being with "p."

        trial_value, negate = self._.transform.is_negate(trial_value)
        if not trial_value.startswith('p.'):
            trial_value = re.escape('p.' + trial_value)
        trial_value = '^{}[A-Z]'.format(trial_value)
        return {kwargs['sample_key']: {'$regex': re.compile(trial_value, re.IGNORECASE)}}, negate

    def mmr_ms_map(self, **kwargs):
        mmr_map = {
            'MMR-Proficient': 'Proficient (MMR-P / MSS)',
            'MMR-Deficient': 'Deficient (MMR-D / MSI-H)',
            'MSI-H': 'Deficient (MMR-D / MSI-H)',
            'MSI-L': 'Proficient (MMR-P / MSS)',
            'MSS': 'Proficient (MMR-P / MSS)'
        }
        trial_value = kwargs['trial_value']
        trial_value, negate = self._.transform.is_negate(trial_value)
        sample_key = kwargs['sample_key']
        sample_value = mmr_map[trial_value]
        return {sample_key: sample_value}, negate


__export__ = ["DFCITransformers"]
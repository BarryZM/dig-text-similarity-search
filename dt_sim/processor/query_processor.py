import os
import sys
import traceback
from time import time
from pickle import dumps as phash
from typing import Dict, List, Tuple, Union

os.environ['OMP_WAIT_POLICY'] = 'PASSIVE'

import numpy as np

from dt_sim.indexer.faiss_cache import faiss_cache
from .base_processor import BaseProcessor, QueryReturn

__all__ = ['QueryProcessor']


DiffScores = List[List[np.float32]]
VectorIDs = List[List[np.int64]]
SentHitPairs = List[Tuple[Union[np.float32, str], str]]
DocPayload = Dict[str, SentHitPairs]
rawScoreIDs = List[Tuple[str, str]]
SortedScoresIDs = List[Dict[str, Union[str, rawScoreIDs]]]


class QueryProcessor(BaseProcessor):

    def __init__(self, index_handler: object, query_vectorizer: object):
        super().__init__()
        self.indexer = index_handler
        self.vectorizer = query_vectorizer
        self.n_queries = 0

    @faiss_cache(1)
    def query_corpus(self, query_str: str, k: int = 5, radius: float = 0.65,
                     start: str = '0000-00-00', end: str = '9999-99-99',
                     rerank_by_doc: bool = True, verbose: bool = True
                     ) -> SortedScoresIDs:
        """
        Vectorize query -> Search faiss index handler -> Format doc payload
        Expects to receive only one query per call.
        :param query_str: Query to vectorize
        :param k: Number of nearest neighboring documents to return
        :param radius: Maximum L2 distance between the query and a result
        :param verbose: Prints time spent on each step
        :param start: Search shards corresponding to this date and beyond
            (Requires shards with names containing an ISO-date-string)
        :param end: Limit date-range search up to this YYYY-MM-DD
        :param rerank_by_doc: Returns all hits within a document (score = best)
        :return: k sorted document hits
        """
        # Vectorize
        t_v = time()
        query_vector = self.vectorize(query_str)

        # Search
        t_s = time()
        scores, faiss_ids = self.indexer.search(query_vector,
                                                k=k, radius=radius,
                                                start=start, end=end)

        # Aggregate hits into docs -> rerank (soon) -> format
        t_p = time()
        doc_hits = self.aggregate_docs(scores, faiss_ids)
        if rerank_by_doc:
            similar_docs = self.format_payload_docs(doc_hits)
        else:
            similar_docs = self.format_payload_singles(doc_hits)

        t_r = time()
        self.n_queries += 1
        if verbose:
            print(f'\n Q: {self.n_queries}'
                  f'\n * Query vectorized in --- {t_s - t_v:0.4f}s' 
                  f'\n * Index searched in ----- {t_p - t_s:0.4f}s'
                  f'\n * Payload formatted in -- {t_r - t_p:0.4f}s')

        return similar_docs[:k]

    def vectorize(self, query: Union[str, List[str]]) -> QueryReturn:
        """
        Use DockerVectorizer for fast Query Vectorization.
        :param query: Text to vectorize
        :return: Formatted query embedding
        """
        if not isinstance(query, list):
            query = [query]
        if len(query) > 1:
            query = query[:1]

        query_vector = self.vectorizer.make_vectors(query)

        if isinstance(query_vector[0], list):
            query_vector = np.array(query_vector, dtype=np.float32)
        return query_vector

    @staticmethod
    def aggregate_docs(scores: DiffScores, faiss_ids: VectorIDs,
                       require_unique_score: bool = True) -> DocPayload:
        """
        Collects outputs from faiss search into document entities.
        :param scores: Faiss query/hit vector L2 distances
        :param faiss_ids: Faiss vector ids
        :param require_unique_score: Discard docs with duplicate sum(scores)
        :return: Dict of docs (key: document id, val: doc with sentence hits)
        """

        def min_diff_cutoff(diff_score, cutoff=0.01) -> str:
            return str(max(diff_score, cutoff))

        def sort_score_ids(sc_ids: rawScoreIDs) -> rawScoreIDs:
            # Checks if already sorted
            if not all(sc_ids[i][0] <= sc_ids[i+1][0] for i in range(len(sc_ids)-1)):
                sc_ids.sort(key=lambda sc_id_tup: sc_id_tup[0])

            return sc_ids

        docs = dict()
        for score, faiss_id in zip(scores[0], faiss_ids[0]):
            if faiss_id > 0:
                doc_id, sent_id = divmod(faiss_id, 10000)
                doc_id = str(doc_id)
                if doc_id not in docs:
                    docs[doc_id] = list()
                docs[doc_id].append((min_diff_cutoff(score), str(faiss_id)))

        if require_unique_score:
            doc_hits = dict()
            unique_doc_scores = set()
            for doc_id, score_ids in docs.items():
                doc_score_hash = phash(sorted([sc_id[0] for sc_id in score_ids]))
                if doc_score_hash not in unique_doc_scores:
                    unique_doc_scores.add(doc_score_hash)
                    doc_hits[doc_id] = sort_score_ids(score_ids)
        else:
            doc_hits = dict()
            for doc_id, score_ids in docs.items():
                doc_hits[doc_id] = sort_score_ids(score_ids)

        return doc_hits

    @staticmethod
    def format_payload_docs(doc_hits: DocPayload) -> SortedScoresIDs:
        """
        :return:
            [
              {
                'doc_id': str(doc_id),
                'id_score_tups': [ (str(faiss_id), str(diff_score)), (.., ..), ... ],
                'score': str(lowest_diff_score)
              },
              ...   # One dict per doc
            ]
        """
        payload = list()
        for doc_id, score_ids in doc_hits.items():
            # Assumes score_ids is a pre-sorted list
            out = dict()
            out['doc_id'] = str(doc_id)
            out['id_score_tups'] = [(str(fid), str(diff)) for diff, fid in score_ids]
            out['score'] = min([diff for diff, _ in score_ids])
            payload.append(out)

        return sorted(payload, key=lambda doc_hit: doc_hit['score'])

    @staticmethod
    def format_payload_singles(doc_hits: DocPayload) -> SortedScoresIDs:
        """
        :return:
            [
              {
                'doc_id': str(doc_id),
                'score': str(diff_score),
                'sentence_id': str(faiss_id)
              },
              ...   # One dict per doc
            ]
        """
        payload = list()
        for doc_id, score_ids in doc_hits.items():
            # Assumes score_ids is a pre-sorted list
            diff_score, faiss_id = score_ids[0]
            out = dict()
            out['doc_id'] = str(doc_id)
            out['score'] = str(diff_score)
            out['sentence_id'] = str(faiss_id)
            payload.append(out)

        return sorted(payload, key=lambda sc_id_dict: sc_id_dict['score'])

    def add_shard(self, shard_path: str):
        """
         Attempts to deploy new shard on current index handler.
        :param shard_path: /full/path/to/shard.index
        """
        if os.path.isfile(shard_path) and shard_path.endswith('.index'):
            try:
                self.indexer.add_shard(shard_path)
            except NameError as e:
                exc_type, exc_val, exc_trace = sys.exc_info()
                lines = traceback.format_exception(exc_type, exc_val, exc_trace)
                print(''.join(lines))
                print(e)
                print(f'Could not add shard: {shard_path}')
        elif not os.path.isfile(shard_path):
            print(f'Error: Path does not specify a file: {shard_path}')
        elif not shard_path.endswith('.index'):
            print(f'Error: Path does not lead to .index: {shard_path}')
        else:
            print(f'Error: Unexpected input: {shard_path}')

    def print_shards(self):
        n_shards = len(self.indexer.paths_to_shards)
        print(f'Faiss Index Shards Deployed: {n_shards}')
        for i, shard_path in enumerate(self.indexer.paths_to_shards, start=1):
            print(f' {i:3d}/{n_shards}: {shard_path}')

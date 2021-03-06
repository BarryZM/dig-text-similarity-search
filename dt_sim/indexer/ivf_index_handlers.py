import re
from time import sleep
from pathlib import Path
from typing import Union
from multiprocessing import Pipe, Process, Queue

import faiss
import numpy as np

from .base_indexer import *
from .faiss_cache import faiss_cache

__all__ = ['DeployShards', 'RangeShards']


class DeployShards(BaseIndexer):
    def __init__(self, shard_dir, nprobe: int = 4):
        """
        For deploying multiple, pre-made IVF indexes as shards.
            (intended for on-disk indexes that do not fit in memory)

        Note: The index shards must be true partitions with no overlapping ids

        :param shard_dir: Dir containing faiss index shards
        :param nprobe: Number of clusters to visit during search
                       (speed accuracy trade-off)
        """
        super().__init__()
        self.paths_to_shards = self.get_index_paths(shard_dir)
        self.nprobe = nprobe

        # Load shards
        shards = list()
        for shard_path in self.paths_to_shards:
            shard = self.load_shard(path_to_shard=shard_path, nprobe=self.nprobe)
            shards.append(shard)

        # Merge shards
        self.index = faiss.IndexShards(512, threaded=True, successive_ids=False)
        for shard in shards:
            self.index.add_shard(shard)

    @staticmethod
    def load_shard(path_to_shard: Union[str, Path], nprobe: int = 4):
        try:
            shard = faiss.read_index(path_to_shard)
        except RuntimeError:
            shard = faiss.read_index(path_to_shard, faiss.IO_FLAG_ONDISK_SAME_DIR)
        shard.nprobe = nprobe
        return shard

    def add_shard(self, new_shard_path: Union[str, Path]):
        if new_shard_path in self.paths_to_shards:
            print('WARNING: This shard is already online \n'
                  '         Aborting...')
            return
        self.paths_to_shards.append(new_shard_path)
        shard = self.load_shard(path_to_shard=new_shard_path, nprobe=self.nprobe)
        self.index.add_shard(shard)


#### Parallelized Nearest Neighbor Search ####
class Shard(Process):
    def __init__(self, shard_name, shard_path: Union[str, Path],
                 input_pipe: Pipe, output_queue: Queue,
                 nprobe: int = 4, daemon: bool = False):
        """ RangeShards search worker """
        super().__init__(name=shard_name)
        self.daemon = daemon

        try:
            self.index = faiss.read_index(shard_path)
        except RuntimeError:
            self.index = faiss.read_index(shard_path, faiss.IO_FLAG_ONDISK_SAME_DIR)
        self.index.nprobe = nprobe
        self.input = input_pipe
        self.output = output_queue

    def run(self):

        @faiss_cache(64)
        def neighborhood(index, query, radius):
            _, ddd, iii = index.range_search(query, radius)
            return ddd, iii

        if self.input.poll():
            (query_vector, radius_limit) = self.input.recv()
            dd, ii = neighborhood(self.index, query_vector, radius_limit)
            self.output.put((dd, ii), block=False)


class RangeShards(BaseIndexer):
    def __init__(self, shard_dir: Union[str, Path], nprobe: int = 4,
                 get_nested: bool = False):
        """
        For deploying multiple, pre-made IVF indexes as shards.
            (intended for on-disk indexes that do not fit in memory)

        Note: The index shards must be true partitions with no overlapping ids

        :param shard_dir: Dir containing faiss index shards
        :param nprobe: Number of clusters to visit during search
                       (speed accuracy trade-off)
        :param get_nested: Load indexes in sub directories of shard_dir
        """
        super().__init__()
        self.paths_to_shards = self.get_index_paths(shard_dir, recursive=get_nested)
        self.nprobe = nprobe
        self.dynamic = True
        self.lock = False
        self.date_seed = str('\d{4}[-/]\d{2}[-/]\d{2}')

        self.results = Queue()
        self.shards = dict()
        self.n_shards = 0
        for shard_path in self.paths_to_shards:
            self.load_shard(shard_path)
        for shard_name, (handler_pipe, shard) in self.shards.items():
            shard.start()
            self.n_shards += 1

    @faiss_cache(256)
    def search(self, query_vector: np.array, k: int, radius: float = 0.65,
               start: str = '0000-00-00', end: str = '9999-99-99'
               ) -> FaissSearch:

        if len(query_vector.shape) < 2 or query_vector.shape[0] > 1:
            query_vector = np.reshape(query_vector, (1, query_vector.shape[0]))

        # Lock search while loading index or actively searching
        while self.lock:
            sleep(1)

        # Lock out other searches
        self.lock = True

        # Start parallel range search
        n_results = 0
        for shard_name, (hpipe, shard) in self.shards.items():
            shard_date = re.search(self.date_seed, shard_name).group()
            if start <= shard_date <= end:
                hpipe.send((query_vector, radius))
                shard.run()
                n_results += 1

        # Aggregate results
        D, I = list(), list()
        while n_results > 0 or not self.results.empty():
            dd, ii = self.results.get()
            D.extend(dd[:k]), I.extend(ii[:k])
            n_results -= 1

        self.lock = False
        return self.joint_sort([D], [I])

    def load_shard(self, shard_path: Union[str, Path]):
        shard_name = str(shard_path).replace('.index', '')
        shard_pipe, handler_pipe = Pipe(False)
        shard = Shard(shard_name, str(shard_path),
                      input_pipe=shard_pipe, output_queue=self.results,
                      nprobe=self.nprobe, daemon=False)
        self.shards[shard_name] = (handler_pipe, shard)

    def add_shard(self, new_shard_path: Union[str, Path]):
        # Lock search while deploying a new shard
        self.lock = True

        shard_name = str(new_shard_path).replace('.index', '')
        if new_shard_path in self.paths_to_shards or \
                shard_name in self.shards:
            print('WARNING: This shard is already online \n'
                  '         Aborting...')
        else:
            self.paths_to_shards.append(new_shard_path)
            self.load_shard(new_shard_path)

        # Release lock
        self.lock = False

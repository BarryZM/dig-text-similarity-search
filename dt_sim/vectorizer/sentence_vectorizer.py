import os
import os.path as p
import json
import requests
from time import time
from pathlib import Path
from typing import List, Union

import tensorflow as tf
import tensorflow_hub as hub

from .base_vectorizer import BaseVectorizer


#### Query Vectorization ####
class DockerVectorizer(BaseVectorizer):
    """
    Intended for fast Query Vectorization.
    Note: Ensure docker container is running before importing class.
    """
    def __init__(self, large: bool = False, model_name: str = None):
        super().__init__()

        if not model_name and large:
            model_name = 'USE-large-v3'
            self.large_USE = True
        elif not model_name:
            model_name = 'USE-lite-v2'
        self.url = f'http://localhost:8501/v1/models/{model_name}:predict'

    def make_vectors(self, query: Union[str, List[str]]):
        """ Takes one query """
        if not isinstance(query, list):
            query = [str(query)]
        elif len(query) > 1:
            query = query[:1]

        payload = {"inputs": {"text": query}}
        payload = json.dumps(payload)

        response = requests.post(self.url, data=payload)
        response.raise_for_status()

        return response.json()['outputs']


#### Corpus Vectorization ####
class SentenceVectorizer(BaseVectorizer):
    """
    Intended for batch Corpus Vectorization
    """
    def __init__(self, large: bool = False, path_to_model: Union[str, Path] = None):
        super().__init__()

        model_parent_dir = p.abspath(p.join(p.dirname(__file__), 'model/'))
        if large:
            model_dir = '96e8f1d3d4d90ce86b2db128249eb8143a91db73/'
            model_url = 'https://tfhub.dev/google/universal-sentence-encoder-large/3'
            self.large_USE = True
        else:
            model_dir = '1fb57c3ffe1a38479233ee9853ddd7a8ac8a8c47/'
            model_url = 'https://tfhub.dev/google/universal-sentence-encoder/2'
        model_path = p.join(model_parent_dir, model_dir)

        if not path_to_model and p.isdir(model_path):
            self.path_to_model = model_path
        elif not path_to_model:
            self.path_to_model = model_url
            os.makedirs(model_parent_dir, exist_ok=True)
            os.environ['TFHUB_CACHE_DIR'] = model_parent_dir
        else:
            self.path_to_model = p.abspath(path_to_model)

        self.graph = None
        self.model = None
        print(f'Loading model: {self.path_to_model}')
        self.define_graph()
        print('Done loading model')
        self.session = None
        print('Initializing TF Session...')
        self.start_session()

    def define_graph(self):
        self.graph = tf.get_default_graph()
        with self.graph.as_default():
            self.model = hub.Module(self.path_to_model)

    def start_session(self):
        self.session = tf.Session()
        with self.graph.as_default():
            self.session.run([tf.global_variables_initializer(), tf.tables_initializer()])

    def close_session(self):
        self.session.close()
        tf.reset_default_graph()
        self.define_graph()

    def make_vectors(self, sentences: Union[str, List[str]], n_minibatch: int = 512,
                     verbose: bool = False) -> List[tf.Tensor]:
        if not isinstance(sentences, list):
            sentences = [sentences]
        i = 0
        t_st = time()
        timing = list()

        embeddings = list()
        batched_tensors = list()
        with self.graph.as_default():
            # High throughput vectorization (fast)
            if len(sentences) > n_minibatch:
                while len(sentences) >= n_minibatch:
                    batch, sentences = list(sentences[:n_minibatch]), list(sentences[n_minibatch:])
                    batched_tensors.append(tf.constant(batch, dtype=tf.string))

                dataset = tf.data.Dataset.from_tensor_slices(batched_tensors)
                dataset = dataset.make_one_shot_iterator()
                make_embeddings = self.model(dataset.get_next())

                while True:
                    try:
                        t_0 = time()
                        embeddings.append(self.session.run(make_embeddings))
                        if verbose:
                            timing.append(time() - t_0)
                            print(f'  ** {i:5d}/{len(batched_tensors)}'
                                  f' : {timing[-1]:3.3f}s :: {sum(timing)/len(timing):3.3f}s avg')
                            i += 1
                    except tf.errors.OutOfRangeError:
                        break

            # Tail end vectorization (slow)
            if len(sentences):
                t_1 = time()
                basic_batch = self.model(sentences)
                embeddings.append(self.session.run(basic_batch))
                if verbose:
                    tm, ts = divmod(time() - t_st, 60)
                    print(f'  ** {i:5d}/{len(batched_tensors)}'
                          f' : {time() - t_1:3.3f}s :: {int(tm)}m{ts:.1f}s tot')
        return embeddings

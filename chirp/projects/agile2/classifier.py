# coding=utf-8
# Copyright 2024 The Perch Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Functions for training and applying a linear classifier."""

import base64
import csv
import dataclasses
import json
from typing import Any, Sequence



from chirp.projects.hoplite import interface as db_interface
#from hoplite.taxonomy import namespace
from ml_collections import config_dict
import numpy as np
import tensorflow as tf
import tqdm


from typing import Any

from chirp.models import metrics
from chirp.projects.agile2 import classifier_data
import tqdm




@dataclasses.dataclass
class LinearClassifier:
  """Wrapper for linear classifier params and metadata."""

  beta: np.ndarray
  beta_bias: np.ndarray
  classes: tuple[str, ...]
  embedding_model_config: Any

  def __call__(self, embeddings: np.ndarray):
    return np.dot(embeddings, self.beta) + self.beta_bias

  def save(self, path: str):
    """Save the classifier to a path."""
    cfg = config_dict.ConfigDict()
    cfg.model_config = self.embedding_model_config
    cfg.classes = self.classes
    # Convert numpy arrays to base64 encoded blobs.
    beta_bytes = base64.b64encode(np.float32(self.beta).tobytes()).decode(
        'ascii'
    )
    beta_bias_bytes = base64.b64encode(
        np.float32(self.beta_bias).tobytes()
    ).decode('ascii')
    cfg.beta = beta_bytes
    cfg.beta_bias = beta_bias_bytes
    with open(path, 'w') as f:
      f.write(cfg.to_json())

  @classmethod
  def load(cls, path: str):
    """Load a classifier from a path."""
    with open(path, 'r') as f:
      cfg_json = json.loads(f.read())
      cfg = config_dict.ConfigDict(cfg_json)
    classes = cfg.classes
    beta = np.frombuffer(base64.b64decode(cfg.beta), dtype=np.float32)
    beta = np.reshape(beta, (-1, len(classes)))
    beta_bias = np.frombuffer(base64.b64decode(cfg.beta_bias), dtype=np.float32)
    embedding_model_config = cfg.model_config
    return cls(beta, beta_bias, classes, embedding_model_config)


def get_linear_model(embedding_dim: int, num_classes: int) -> tf.keras.Model:
  """Create a simple linear Keras model."""
  model = tf.keras.Sequential([
      tf.keras.Input(shape=[embedding_dim]),
      tf.keras.layers.Dense(num_classes),
  ])
  return model


def bce_loss(
    y_true: tf.Tensor,
    logits: tf.Tensor,
    is_labeled_mask: tf.Tensor,
    weak_neg_weight: float,
) -> tf.Tensor:
  """Binary cross entropy loss from logits with weak negative weights."""
  y_true = tf.cast(y_true, dtype=logits.dtype)
  log_p = tf.math.log_sigmoid(logits)
  log_not_p = tf.math.log_sigmoid(-logits)
  raw_bce = -y_true * log_p + (1.0 - y_true) * log_not_p
  is_labeled_mask = tf.cast(is_labeled_mask, dtype=logits.dtype)
  weights = (1.0 - is_labeled_mask) * weak_neg_weight + is_labeled_mask
  return tf.reduce_mean(raw_bce * weights)


def infer(params, embeddings: np.ndarray):
  """Apply the model to embeddings."""
  return np.dot(embeddings, params['beta']) + params['beta_bias']


def eval_classifier(
    params: Any,
    data_manager: classifier_data.DataManager,
    eval_ids: np.ndarray,
) -> dict[str, float]:
  """Evaluate a classifier on a set of examples."""
  iter_ = data_manager.batched_example_iterator(
      eval_ids, add_weak_negatives=False, repeat=False
  )
  # The embedding ids may be shuffled by the iterator, so we will track the ids
  # of the examples we are evaluating.
  got_ids = []
  pred_logits = []
  true_labels = []
  for batch in iter_:
    pred_logits.append(infer(params, batch.embedding))
    true_labels.append(batch.multihot)
    got_ids.append(batch.idx)
  pred_logits = np.concatenate(pred_logits, axis=0)
  true_labels = np.concatenate(true_labels, axis=0)
  got_ids = np.concatenate(got_ids, axis=0)

  # Compute the top1 accuracy on examples with at least one label.
  labeled_locs = np.where(true_labels.sum(axis=1) > 0)
  top_preds = np.argmax(pred_logits, axis=1)
  top1 = true_labels[np.arange(top_preds.shape[0]), top_preds]
  top1 = top1[labeled_locs].mean()

  rocs = metrics.roc_auc(
      logits=pred_logits, labels=true_labels, sample_threshold=1
  )
  cmaps = metrics.cmap(
      logits=pred_logits, labels=true_labels, sample_threshold=1
  )
  return {
      'top1_acc': top1,
      'roc_auc': rocs['macro'],
      'roc_auc_individual': rocs['individual'],
      'cmap': cmaps['macro'],
      'cmap_individual': cmaps['individual'],
      'eval_ids': got_ids,
      'eval_preds': pred_logits,
      'eval_labels': true_labels,
  }


def train_linear_classifier(
    data_manager: classifier_data.DataManager,
    learning_rate: float,
    weak_neg_weight: float,
    num_train_steps: int,
) -> tuple[LinearClassifier, dict[str, float]]:
  """Train a linear classifier."""
  embedding_dim = data_manager.db.embedding_dimension()
  num_classes = len(data_manager.get_target_labels())
  lin_model = get_linear_model(embedding_dim, num_classes)
  optimizer = tf.keras.optimizers.Adam(learning_rate=learning_rate)
  lin_model.compile(optimizer=optimizer, loss='binary_crossentropy')

  @tf.function
  def train_step(y_true, embeddings, is_labeled_mask):
    with tf.GradientTape() as tape:
      logits = lin_model(embeddings, training=True)
      loss = bce_loss(y_true, logits, is_labeled_mask, weak_neg_weight)
      loss = tf.reduce_mean(loss)
    grads = tape.gradient(loss, lin_model.trainable_variables)
    optimizer.apply_gradients(zip(grads, lin_model.trainable_variables))
    return loss

  train_idxes, eval_idxes = data_manager.get_train_test_split()
  train_iter_ = data_manager.batched_example_iterator(
      train_idxes, add_weak_negatives=True, repeat=True
  )
  progress = tqdm.tqdm(enumerate(train_iter_), total=num_train_steps)
  update_steps = set([int(b * num_train_steps / 100) for b in range(100)])

  for step, batch in enumerate(train_iter_):
    if step >= num_train_steps:
      break
    step_loss = train_step(
        batch.multihot, batch.embedding, batch.is_labeled_mask
    )
    if step in update_steps:
      progress.update(n=num_train_steps // 100)
      progress.set_description(f'Loss {step_loss:.8f}')
  progress.clear()
  progress.close()

  params = {
      'beta': lin_model.get_weights()[0],
      'beta_bias': lin_model.get_weights()[1],
  }
  eval_scores = eval_classifier(params, data_manager, eval_idxes)

  model_config = data_manager.db.get_metadata('model_config')
  linear_classifier = LinearClassifier(
      beta=params['beta'],
      beta_bias=params['beta_bias'],
      classes=data_manager.get_target_labels(),
      embedding_model_config=model_config,
  )
  return linear_classifier, eval_scores


def write_inference_csv(
    linear_classifier: LinearClassifier,
    db: db_interface.GraphSearchDBInterface,
    output_filepath: str,
    threshold: float,
    labels: Sequence[str] | None = None,
    dataset: str | None = None,
    row_func: Any = None,
):
  """Write a CSV for all audio windows with logits above a threshold.

  Args:
    linear_classifier: Trained LinearClassifier to use for inference, containing beta, beta_bias, and classes.
    db: GraphSearchDBInterface to read embeddings from.
    output_filepath: Path to write the CSV to.
    threshold: Logits must be above this value to be written.
    labels: If provided, only write logits for these labels. If None, write
      logits for all labels.
    dataset: If provided, only write logits for embeddings from this dataset.
    row_func: If provided, a function that returns additional columns to write. This function accepts an optional
              argument, the row, and returns a list of values for additional columns. If the row is not provided, it will
              return the header for the additional columns.

  Returns:
    None
  """
  idxes = db.get_embedding_ids(dataset=dataset)
  if labels is None:
    labels = linear_classifier.classes
  label_ids = {cl: i for i, cl in enumerate(linear_classifier.classes)}
  target_label_ids = np.array([label_ids[l] for l in labels])
  logits_fn = lambda emb: linear_classifier(emb)[target_label_ids]

  with open(output_filepath, 'w', newline='') as f:
      writer = csv.writer(f)
      # Write header
      header = ['idx', 'dataset_name', 'source_id', 'offset', 'label', 'logits']
      if row_func is not None:
          header += row_func()
      writer.writerow(header)
      
      # Write data
      for idx in tqdm.tqdm(idxes):
          source = db.get_embedding_source(idx)
          emb = db.get_embedding(idx)
          logits = logits_fn(emb)
          for a in np.argwhere(logits > threshold):
              lbl = labels[int(a)]
              row = [
                  idx,
                  source.dataset_name,
                  source.source_id,
                  source.offsets[0],
                  lbl,
                  logits[a],
              ]
              if row_func is not None:
                  row += row_func(row)
              writer.writerow(row)


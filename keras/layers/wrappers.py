# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
# pylint: disable=protected-access
# pylint: disable=g-classes-have-attributes
"""Wrapper layers: layers that augment the functionality of another layer."""

import tensorflow.compat.v2 as tf

import copy
from keras import backend
from keras.engine.base_layer import Layer
from keras.engine.input_spec import InputSpec
from keras.layers.recurrent import _standardize_args
from keras.utils import generic_utils
from keras.utils import layer_utils
from keras.utils import tf_inspect
from keras.utils import tf_utils
from tensorflow.python.util.tf_export import keras_export


@keras_export('keras.layers.Wrapper')
class Wrapper(Layer):
  """Abstract wrapper base class.

  Wrappers take another layer and augment it in various ways.
  Do not use this class as a layer, it is only an abstract base class.
  Two usable wrappers are the `TimeDistributed` and `Bidirectional` wrappers.

  Args:
    layer: The layer to be wrapped.
  """

  def __init__(self, layer, **kwargs):
    assert isinstance(layer, Layer)
    self.layer = layer
    super(Wrapper, self).__init__(**kwargs)

  def build(self, input_shape=None):
    if not self.layer.built:
      self.layer.build(input_shape)
      self.layer.built = True
    self.built = True

  @property
  def activity_regularizer(self):
    if hasattr(self.layer, 'activity_regularizer'):
      return self.layer.activity_regularizer
    else:
      return None

  def get_config(self):
    config = {'layer': generic_utils.serialize_keras_object(self.layer)}
    base_config = super(Wrapper, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  @classmethod
  def from_config(cls, config, custom_objects=None):
    from keras.layers import deserialize as deserialize_layer  # pylint: disable=g-import-not-at-top
    # Avoid mutating the input dict
    config = copy.deepcopy(config)
    layer = deserialize_layer(
        config.pop('layer'), custom_objects=custom_objects)
    return cls(layer, **config)


@keras_export('keras.layers.TimeDistributed')
class TimeDistributed(Wrapper):
  """This wrapper allows to apply a layer to every temporal slice of an input.

  Every input should be at least 3D, and the dimension of index one of the
  first input will be considered to be the temporal dimension.

  Consider a batch of 32 video samples, where each sample is a 128x128 RGB image
  with `channels_last` data format, across 10 timesteps.
  The batch input shape is `(32, 10, 128, 128, 3)`.

  You can then use `TimeDistributed` to apply the same `Conv2D` layer to each
  of the 10 timesteps, independently:

  >>> inputs = tf.keras.Input(shape=(10, 128, 128, 3))
  >>> conv_2d_layer = tf.keras.layers.Conv2D(64, (3, 3))
  >>> outputs = tf.keras.layers.TimeDistributed(conv_2d_layer)(inputs)
  >>> outputs.shape
  TensorShape([None, 10, 126, 126, 64])

  Because `TimeDistributed` applies the same instance of `Conv2D` to each of the
  timestamps, the same set of weights are used at each timestamp.

  Args:
    layer: a `tf.keras.layers.Layer` instance.

  Call arguments:
    inputs: Input tensor of shape (batch, time, ...) or nested tensors,
      and each of which has shape (batch, time, ...).
    training: Python boolean indicating whether the layer should behave in
      training mode or in inference mode. This argument is passed to the
      wrapped layer (only if the layer supports this argument).
    mask: Binary tensor of shape `(samples, timesteps)` indicating whether
      a given timestep should be masked. This argument is passed to the
      wrapped layer (only if the layer supports this argument).

  Raises:
    ValueError: If not initialized with a `tf.keras.layers.Layer` instance.
  """

  def __init__(self, layer, **kwargs):
    if not isinstance(layer, Layer):
      raise ValueError(
          'Please initialize `TimeDistributed` layer with a '
          '`tf.keras.layers.Layer` instance. You passed: {input}'.format(
              input=layer))
    super(TimeDistributed, self).__init__(layer, **kwargs)
    self.supports_masking = True

    # It is safe to use the fast, reshape-based approach with all of our
    # built-in Layers.
    self._always_use_reshape = (
        layer_utils.is_builtin_layer(layer) and
        not getattr(layer, 'stateful', False))

  def _get_shape_tuple(self, init_tuple, tensor, start_idx, int_shape=None):
    """Finds non-specific dimensions in the static shapes.

    The static shapes are replaced with the corresponding dynamic shapes of the
    tensor.
    Args:
      init_tuple: a tuple, the first part of the output shape
      tensor: the tensor from which to get the (static and dynamic) shapes
        as the last part of the output shape
      start_idx: int, which indicate the first dimension to take from
        the static shape of the tensor
      int_shape: an alternative static shape to take as the last part
        of the output shape
    Returns:
      The new int_shape with the first part from init_tuple
      and the last part from either `int_shape` (if provided)
      or `tensor.shape`, where every `None` is replaced by
      the corresponding dimension from `tf.shape(tensor)`.
    """
    # replace all None in int_shape by backend.shape
    if int_shape is None:
      int_shape = backend.int_shape(tensor)[start_idx:]
    if isinstance(int_shape, tf.TensorShape):
      int_shape = int_shape.as_list()
    if not any(not s for s in int_shape):
      return init_tuple + tuple(int_shape)
    shape = backend.shape(tensor)
    int_shape = list(int_shape)
    for i, s in enumerate(int_shape):
      if not s:
        int_shape[i] = shape[start_idx + i]
    return init_tuple + tuple(int_shape)

  def _remove_timesteps(self, dims):
    dims = dims.as_list()
    return tf.TensorShape([dims[0]] + dims[2:])

  def build(self, input_shape):
    input_shape = tf_utils.convert_shapes(input_shape, to_tuples=False)
    input_dims = tf.nest.flatten(
        tf.nest.map_structure(lambda x: x.ndims, input_shape))
    if any(dim < 3 for dim in input_dims):
      raise ValueError(
          '`TimeDistributed` Layer should be passed an `input_shape ` '
          'with at least 3 dimensions, received: ' + str(input_shape))
    # Don't enforce the batch or time dimension.
    self.input_spec = tf.nest.map_structure(
        lambda x: InputSpec(shape=[None, None] + x.as_list()[2:]), input_shape)
    child_input_shape = tf.nest.map_structure(self._remove_timesteps, input_shape)
    child_input_shape = tf_utils.convert_shapes(child_input_shape)
    super(TimeDistributed, self).build(tuple(child_input_shape))
    self.built = True

  def compute_output_shape(self, input_shape):
    input_shape = tf_utils.convert_shapes(input_shape, to_tuples=False)

    child_input_shape = tf.nest.map_structure(self._remove_timesteps, input_shape)
    child_output_shape = self.layer.compute_output_shape(child_input_shape)
    child_output_shape = tf_utils.convert_shapes(
        child_output_shape, to_tuples=False)
    timesteps = tf_utils.convert_shapes(input_shape)
    timesteps = tf.nest.flatten(timesteps)[1]

    def insert_timesteps(dims):
      dims = dims.as_list()
      return tf.TensorShape([dims[0], timesteps] + dims[1:])

    return tf.nest.map_structure(insert_timesteps, child_output_shape)

  def call(self, inputs, training=None, mask=None):
    kwargs = {}
    if generic_utils.has_arg(self.layer.call, 'training'):
      kwargs['training'] = training

    input_shape = tf.nest.map_structure(
        lambda x: tf.TensorShape(backend.int_shape(x)), inputs)
    batch_size = tf_utils.convert_shapes(input_shape)
    batch_size = tf.nest.flatten(batch_size)[0]
    if batch_size and not self._always_use_reshape:
      inputs, row_lengths = backend.convert_inputs_if_ragged(inputs)
      is_ragged_input = row_lengths is not None
      input_length = tf_utils.convert_shapes(input_shape)
      input_length = tf.nest.flatten(input_length)[1]

      # batch size matters, use rnn-based implementation
      def step(x, _):
        output = self.layer(x, **kwargs)
        return output, []

      _, outputs, _ = backend.rnn(
          step,
          inputs,
          initial_states=[],
          input_length=row_lengths[0] if is_ragged_input else input_length,
          mask=mask,
          unroll=False)
      # pylint: disable=g-long-lambda
      y = tf.nest.map_structure(
          lambda output: backend.maybe_convert_to_ragged(
              is_ragged_input, output, row_lengths), outputs)
    else:
      # No batch size specified, therefore the layer will be able
      # to process batches of any size.
      # We can go with reshape-based implementation for performance.
      is_ragged_input = tf.nest.map_structure(
          lambda x: isinstance(x, tf.RaggedTensor), inputs)
      is_ragged_input = tf.nest.flatten(is_ragged_input)
      if all(is_ragged_input):
        input_values = tf.nest.map_structure(lambda x: x.values, inputs)
        input_row_lenghts = tf.nest.map_structure(
            lambda x: x.nested_row_lengths()[0], inputs)
        y = self.layer(input_values, **kwargs)
        y = tf.nest.map_structure(tf.RaggedTensor.from_row_lengths, y,
                               input_row_lenghts)
      elif any(is_ragged_input):
        raise ValueError('All inputs has to be either ragged or not, '
                         'but not mixed. You passed: {}'.format(inputs))
      else:
        input_length = tf_utils.convert_shapes(input_shape)
        input_length = tf.nest.flatten(input_length)[1]
        if not input_length:
          input_length = tf.nest.map_structure(lambda x: tf.compat.v1.shape(x)[1],
                                            inputs)
          input_length = generic_utils.to_list(tf.nest.flatten(input_length))[0]

        inner_input_shape = tf.nest.map_structure(
            lambda x: self._get_shape_tuple((-1,), x, 2), inputs)
        # Shape: (num_samples * timesteps, ...). And track the
        # transformation in self._input_map.
        inputs = tf.__internal__.nest.map_structure_up_to(inputs, tf.reshape, inputs,
                                          inner_input_shape)
        # (num_samples * timesteps, ...)
        if generic_utils.has_arg(self.layer.call, 'mask') and mask is not None:
          inner_mask_shape = self._get_shape_tuple((-1,), mask, 2)
          kwargs['mask'] = backend.reshape(mask, inner_mask_shape)

        y = self.layer(inputs, **kwargs)

        # Shape: (num_samples, timesteps, ...)
        output_shape = self.compute_output_shape(input_shape)
        # pylint: disable=g-long-lambda
        output_shape = tf.nest.map_structure(
            lambda tensor, int_shape: self._get_shape_tuple(
                (-1, input_length), tensor, 1, int_shape[2:]), y, output_shape)
        y = tf.__internal__.nest.map_structure_up_to(y, tf.reshape, y, output_shape)
        if not tf.executing_eagerly():
          # Set the static shape for the result since it might be lost during
          # array_ops reshape, eg, some `None` dim in the result could be
          # inferred.
          tf.__internal__.nest.map_structure_up_to(
              y, lambda tensor, shape: tensor.set_shape(shape), y,
              self.compute_output_shape(input_shape))

    return y

  def compute_mask(self, inputs, mask=None):
    """Computes an output mask tensor for Embedding layer.

    This is based on the inputs, mask, and the inner layer.
    If batch size is specified:
    Simply return the input `mask`. (An rnn-based implementation with
    more than one rnn inputs is required but not supported in tf.keras yet.)
    Otherwise we call `compute_mask` of the inner layer at each time step.
    If the output mask at each time step is not `None`:
    (E.g., inner layer is Masking or RNN)
    Concatenate all of them and return the concatenation.
    If the output mask at each time step is `None` and the input mask is not
    `None`:(E.g., inner layer is Dense)
    Reduce the input_mask to 2 dimensions and return it.
    Otherwise (both the output mask and the input mask are `None`):
    (E.g., `mask` is not used at all)
    Return `None`.

    Args:
      inputs: Tensor with shape [batch size, timesteps, ...] indicating the
        input to TimeDistributed. If static shape information is available for
        "batch size", `mask` is returned unmodified.
      mask: Either None (indicating no masking) or a Tensor indicating the
        input mask for TimeDistributed. The shape can be static or dynamic.

    Returns:
      Either None (no masking), or a [batch size, timesteps, ...] Tensor with
      an output mask for the TimeDistributed layer with the shape beyond the
      second dimension being the value of the input mask shape(if the computed
      output mask is none), an output mask with the shape beyond the first
      dimension being the value of the mask shape(if mask is not None) or
      output mask with the shape beyond the first dimension being the
      value of the computed output shape.

    """
    # cases need to call the layer.compute_mask when input_mask is None:
    # Masking layer and Embedding layer with mask_zero
    input_shape = tf.nest.map_structure(
        lambda x: tf.TensorShape(backend.int_shape(x)), inputs)
    input_shape = tf_utils.convert_shapes(input_shape, to_tuples=False)
    batch_size = tf_utils.convert_shapes(input_shape)
    batch_size = tf.nest.flatten(batch_size)[0]
    is_ragged_input = tf.nest.map_structure(
        lambda x: isinstance(x, tf.RaggedTensor), inputs)
    is_ragged_input = generic_utils.to_list(tf.nest.flatten(is_ragged_input))
    if batch_size and not self._always_use_reshape or any(is_ragged_input):
      # batch size matters, we currently do not handle mask explicitly, or if
      # the layer always uses reshape approach, or the input is a ragged tensor.
      return mask
    inner_mask = mask
    if inner_mask is not None:
      inner_mask_shape = self._get_shape_tuple((-1,), mask, 2)
      inner_mask = backend.reshape(inner_mask, inner_mask_shape)
    inner_input_shape = tf.nest.map_structure(
        lambda tensor: self._get_shape_tuple((-1,), tensor, 2), inputs)
    inner_inputs = tf.__internal__.nest.map_structure_up_to(inputs, tf.reshape, inputs,
                                            inner_input_shape)
    output_mask = self.layer.compute_mask(inner_inputs, inner_mask)
    if output_mask is None:
      if mask is None:
        return None
      # input_mask is not None, and output_mask is None:
      # we should return a not-None mask
      output_mask = mask
      for _ in range(2, len(backend.int_shape(mask))):
        output_mask = backend.any(output_mask, axis=-1)
    else:
      # output_mask is not None. We need to reshape it
      input_length = tf_utils.convert_shapes(input_shape)
      input_length = tf.nest.flatten(input_length)[1]
      if not input_length:
        input_length = tf.nest.map_structure(lambda x: backend.shape(x)[1], inputs)
        input_length = tf.nest.flatten(input_length)[0]
      output_mask_int_shape = backend.int_shape(output_mask)
      if output_mask_int_shape is None:
        # if the output_mask does not have a static shape,
        # its shape must be the same as mask's
        if mask is not None:
          output_mask_int_shape = backend.int_shape(mask)
        else:
          input_shape = generic_utils.to_list(tf.nest.flatten(input_shape))[0]
          output_mask_int_shape = backend.compute_output_shape(input_shape)[:-1]
      output_mask_shape = self._get_shape_tuple(
          (-1, input_length), output_mask, 1, output_mask_int_shape[1:])
      output_mask = backend.reshape(output_mask, output_mask_shape)
    return output_mask


@keras_export('keras.layers.Bidirectional')
class Bidirectional(Wrapper):
  """Bidirectional wrapper for RNNs.

  Args:
    layer: `keras.layers.RNN` instance, such as `keras.layers.LSTM` or
      `keras.layers.GRU`. It could also be a `keras.layers.Layer` instance
      that meets the following criteria:
      1. Be a sequence-processing layer (accepts 3D+ inputs).
      2. Have a `go_backwards`, `return_sequences` and `return_state`
        attribute (with the same semantics as for the `RNN` class).
      3. Have an `input_spec` attribute.
      4. Implement serialization via `get_config()` and `from_config()`.
      Note that the recommended way to create new RNN layers is to write a
      custom RNN cell and use it with `keras.layers.RNN`, instead of
      subclassing `keras.layers.Layer` directly.
      - When the `returns_sequences` is true, the output of the masked timestep
      will be zero regardless of the layer's original `zero_output_for_mask`
      value.
    merge_mode: Mode by which outputs of the forward and backward RNNs will be
      combined. One of {'sum', 'mul', 'concat', 'ave', None}. If None, the
      outputs will not be combined, they will be returned as a list. Default
      value is 'concat'.
    backward_layer: Optional `keras.layers.RNN`, or `keras.layers.Layer`
      instance to be used to handle backwards input processing.
      If `backward_layer` is not provided, the layer instance passed as the
      `layer` argument will be used to generate the backward layer
      automatically.
      Note that the provided `backward_layer` layer should have properties
      matching those of the `layer` argument, in particular it should have the
      same values for `stateful`, `return_states`, `return_sequences`, etc.
      In addition, `backward_layer` and `layer` should have different
      `go_backwards` argument values.
      A `ValueError` will be raised if these requirements are not met.

  Call arguments:
    The call arguments for this layer are the same as those of the wrapped RNN
      layer.
    Beware that when passing the `initial_state` argument during the call of
    this layer, the first half in the list of elements in the `initial_state`
    list will be passed to the forward RNN call and the last half in the list
    of elements will be passed to the backward RNN call.

  Raises:
    ValueError:
      1. If `layer` or `backward_layer` is not a `Layer` instance.
      2. In case of invalid `merge_mode` argument.
      3. If `backward_layer` has mismatched properties compared to `layer`.

  Examples:

  ```python
  model = Sequential()
  model.add(Bidirectional(LSTM(10, return_sequences=True), input_shape=(5, 10)))
  model.add(Bidirectional(LSTM(10)))
  model.add(Dense(5))
  model.add(Activation('softmax'))
  model.compile(loss='categorical_crossentropy', optimizer='rmsprop')

   # With custom backward layer
   model = Sequential()
   forward_layer = LSTM(10, return_sequences=True)
   backward_layer = LSTM(10, activation='relu', return_sequences=True,
                         go_backwards=True)
   model.add(Bidirectional(forward_layer, backward_layer=backward_layer,
                           input_shape=(5, 10)))
   model.add(Dense(5))
   model.add(Activation('softmax'))
   model.compile(loss='categorical_crossentropy', optimizer='rmsprop')
  ```
  """

  def __init__(self,
               layer,
               merge_mode='concat',
               weights=None,
               backward_layer=None,
               **kwargs):
    if not isinstance(layer, Layer):
      raise ValueError(
          'Please initialize `Bidirectional` layer with a '
          '`Layer` instance. You passed: {input}'.format(input=layer))
    if backward_layer is not None and not isinstance(backward_layer, Layer):
      raise ValueError('`backward_layer` need to be a `Layer` instance. '
                       'You passed: {input}'.format(input=backward_layer))
    if merge_mode not in ['sum', 'mul', 'ave', 'concat', None]:
      raise ValueError('Invalid merge mode. '
                       'Merge mode should be one of '
                       '{"sum", "mul", "ave", "concat", None}')
    # We don't want to track `layer` since we're already tracking the two copies
    # of it we actually run.
    self._setattr_tracking = False
    super(Bidirectional, self).__init__(layer, **kwargs)
    self._setattr_tracking = True

    # Recreate the forward layer from the original layer config, so that it will
    # not carry over any state from the layer.
    self.forward_layer = self._recreate_layer_from_config(layer)

    if backward_layer is None:
      self.backward_layer = self._recreate_layer_from_config(
          layer, go_backwards=True)
    else:
      self.backward_layer = backward_layer
      # Keep the custom backward layer config, so that we can save it later. The
      # layer's name might be updated below with prefix 'backward_', and we want
      # to preserve the original config.
      self._backward_layer_config = generic_utils.serialize_keras_object(
          backward_layer)

    self.forward_layer._name = 'forward_' + self.forward_layer.name
    self.backward_layer._name = 'backward_' + self.backward_layer.name

    self._verify_layer_config()

    def force_zero_output_for_mask(layer):
      # Force the zero_output_for_mask to be True if returning sequences.
      if getattr(layer, 'zero_output_for_mask', None) is not None:
        layer.zero_output_for_mask = layer.return_sequences

    force_zero_output_for_mask(self.forward_layer)
    force_zero_output_for_mask(self.backward_layer)

    self.merge_mode = merge_mode
    if weights:
      nw = len(weights)
      self.forward_layer.initial_weights = weights[:nw // 2]
      self.backward_layer.initial_weights = weights[nw // 2:]
    self.stateful = layer.stateful
    self.return_sequences = layer.return_sequences
    self.return_state = layer.return_state
    self.supports_masking = True
    self._trainable = True
    self._num_constants = 0
    self.input_spec = layer.input_spec

  def _verify_layer_config(self):
    """Ensure the forward and backward layers have valid common property."""
    if self.forward_layer.go_backwards == self.backward_layer.go_backwards:
      raise ValueError('Forward layer and backward layer should have different '
                       '`go_backwards` value.')

    common_attributes = ('stateful', 'return_sequences', 'return_state')
    for a in common_attributes:
      forward_value = getattr(self.forward_layer, a)
      backward_value = getattr(self.backward_layer, a)
      if forward_value != backward_value:
        raise ValueError(
            'Forward layer and backward layer are expected to have the same '
            'value for attribute {attr}, got {forward} and {backward}'.format(
                attr=a, forward=forward_value, backward=backward_value))

  def _recreate_layer_from_config(self, layer, go_backwards=False):
    # When recreating the layer from its config, it is possible that the layer
    # is a RNN layer that contains custom cells. In this case we inspect the
    # layer and pass the custom cell class as part of the `custom_objects`
    # argument when calling `from_config`.
    # See https://github.com/tensorflow/tensorflow/issues/26581 for more detail.
    config = layer.get_config()
    if go_backwards:
      config['go_backwards'] = not config['go_backwards']
    if 'custom_objects' in tf_inspect.getfullargspec(
        layer.__class__.from_config).args:
      custom_objects = {}
      cell = getattr(layer, 'cell', None)
      if cell is not None:
        custom_objects[cell.__class__.__name__] = cell.__class__
        # For StackedRNNCells
        stacked_cells = getattr(cell, 'cells', [])
        for c in stacked_cells:
          custom_objects[c.__class__.__name__] = c.__class__
      return layer.__class__.from_config(config, custom_objects=custom_objects)
    else:
      return layer.__class__.from_config(config)

  @tf_utils.shape_type_conversion
  def compute_output_shape(self, input_shape):
    output_shape = self.forward_layer.compute_output_shape(input_shape)
    if self.return_state:
      state_shape = tf_utils.convert_shapes(output_shape[1:], to_tuples=False)
      output_shape = tf_utils.convert_shapes(output_shape[0], to_tuples=False)
    else:
      output_shape = tf_utils.convert_shapes(output_shape, to_tuples=False)

    if self.merge_mode == 'concat':
      output_shape = output_shape.as_list()
      output_shape[-1] *= 2
      output_shape = tf.TensorShape(output_shape)
    elif self.merge_mode is None:
      output_shape = [output_shape, copy.copy(output_shape)]

    if self.return_state:
      if self.merge_mode is None:
        return output_shape + state_shape + copy.copy(state_shape)
      return [output_shape] + state_shape + copy.copy(state_shape)
    return output_shape

  def __call__(self, inputs, initial_state=None, constants=None, **kwargs):
    """`Bidirectional.__call__` implements the same API as the wrapped `RNN`."""
    inputs, initial_state, constants = _standardize_args(
        inputs, initial_state, constants, self._num_constants)

    if isinstance(inputs, list):
      if len(inputs) > 1:
        initial_state = inputs[1:]
      inputs = inputs[0]

    if initial_state is None and constants is None:
      return super(Bidirectional, self).__call__(inputs, **kwargs)

    # Applies the same workaround as in `RNN.__call__`
    additional_inputs = []
    additional_specs = []
    if initial_state is not None:
      # Check if `initial_state` can be splitted into half
      num_states = len(initial_state)
      if num_states % 2 > 0:
        raise ValueError(
            'When passing `initial_state` to a Bidirectional RNN, '
            'the state should be a list containing the states of '
            'the underlying RNNs. '
            'Found: ' + str(initial_state))

      kwargs['initial_state'] = initial_state
      additional_inputs += initial_state
      state_specs = tf.nest.map_structure(
          lambda state: InputSpec(shape=backend.int_shape(state)),
          initial_state)
      self.forward_layer.state_spec = state_specs[:num_states // 2]
      self.backward_layer.state_spec = state_specs[num_states // 2:]
      additional_specs += state_specs
    if constants is not None:
      kwargs['constants'] = constants
      additional_inputs += constants
      constants_spec = [InputSpec(shape=backend.int_shape(constant))
                        for constant in constants]
      self.forward_layer.constants_spec = constants_spec
      self.backward_layer.constants_spec = constants_spec
      additional_specs += constants_spec

      self._num_constants = len(constants)
      self.forward_layer._num_constants = self._num_constants
      self.backward_layer._num_constants = self._num_constants

    is_keras_tensor = backend.is_keras_tensor(
        tf.nest.flatten(additional_inputs)[0])
    for tensor in tf.nest.flatten(additional_inputs):
      if backend.is_keras_tensor(tensor) != is_keras_tensor:
        raise ValueError('The initial state of a Bidirectional'
                         ' layer cannot be specified with a mix of'
                         ' Keras tensors and non-Keras tensors'
                         ' (a "Keras tensor" is a tensor that was'
                         ' returned by a Keras layer, or by `Input`)')

    if is_keras_tensor:
      # Compute the full input spec, including state
      full_input = [inputs] + additional_inputs
      # The original input_spec is None since there could be a nested tensor
      # input. Update the input_spec to match the inputs.
      full_input_spec = [None for _ in range(len(tf.nest.flatten(inputs)))
                        ] + additional_specs
      # Removing kwargs since the value are passed with input list.
      kwargs['initial_state'] = None
      kwargs['constants'] = None

      # Perform the call with temporarily replaced input_spec
      original_input_spec = self.input_spec
      self.input_spec = full_input_spec
      output = super(Bidirectional, self).__call__(full_input, **kwargs)
      self.input_spec = original_input_spec
      return output
    else:
      return super(Bidirectional, self).__call__(inputs, **kwargs)

  def call(self,
           inputs,
           training=None,
           mask=None,
           initial_state=None,
           constants=None):
    """`Bidirectional.call` implements the same API as the wrapped `RNN`."""
    kwargs = {}
    if generic_utils.has_arg(self.layer.call, 'training'):
      kwargs['training'] = training
    if generic_utils.has_arg(self.layer.call, 'mask'):
      kwargs['mask'] = mask
    if generic_utils.has_arg(self.layer.call, 'constants'):
      kwargs['constants'] = constants

    if generic_utils.has_arg(self.layer.call, 'initial_state'):
      if isinstance(inputs, list) and len(inputs) > 1:
        # initial_states are keras tensors, which means they are passed in
        # together with inputs as list. The initial_states need to be split into
        # forward and backward section, and be feed to layers accordingly.
        forward_inputs = [inputs[0]]
        backward_inputs = [inputs[0]]
        pivot = (len(inputs) - self._num_constants) // 2 + 1
        # add forward initial state
        forward_inputs += inputs[1:pivot]
        if not self._num_constants:
          # add backward initial state
          backward_inputs += inputs[pivot:]
        else:
          # add backward initial state
          backward_inputs += inputs[pivot:-self._num_constants]
          # add constants for forward and backward layers
          forward_inputs += inputs[-self._num_constants:]
          backward_inputs += inputs[-self._num_constants:]
        forward_state, backward_state = None, None
        if 'constants' in kwargs:
          kwargs['constants'] = None
      elif initial_state is not None:
        # initial_states are not keras tensors, eg eager tensor from np array.
        # They are only passed in from kwarg initial_state, and should be passed
        # to forward/backward layer via kwarg initial_state as well.
        forward_inputs, backward_inputs = inputs, inputs
        half = len(initial_state) // 2
        forward_state = initial_state[:half]
        backward_state = initial_state[half:]
      else:
        forward_inputs, backward_inputs = inputs, inputs
        forward_state, backward_state = None, None

      y = self.forward_layer(forward_inputs,
                             initial_state=forward_state, **kwargs)
      y_rev = self.backward_layer(backward_inputs,
                                  initial_state=backward_state, **kwargs)
    else:
      y = self.forward_layer(inputs, **kwargs)
      y_rev = self.backward_layer(inputs, **kwargs)

    if self.return_state:
      states = y[1:] + y_rev[1:]
      y = y[0]
      y_rev = y_rev[0]

    if self.return_sequences:
      time_dim = 0 if getattr(self.forward_layer, 'time_major', False) else 1
      y_rev = backend.reverse(y_rev, time_dim)
    if self.merge_mode == 'concat':
      output = backend.concatenate([y, y_rev])
    elif self.merge_mode == 'sum':
      output = y + y_rev
    elif self.merge_mode == 'ave':
      output = (y + y_rev) / 2
    elif self.merge_mode == 'mul':
      output = y * y_rev
    elif self.merge_mode is None:
      output = [y, y_rev]
    else:
      raise ValueError(
          'Unrecognized value for `merge_mode`: %s' % (self.merge_mode))

    if self.return_state:
      if self.merge_mode is None:
        return output + states
      return [output] + states
    return output

  def reset_states(self):
    self.forward_layer.reset_states()
    self.backward_layer.reset_states()

  def build(self, input_shape):
    with backend.name_scope(self.forward_layer.name):
      self.forward_layer.build(input_shape)
    with backend.name_scope(self.backward_layer.name):
      self.backward_layer.build(input_shape)
    self.built = True

  def compute_mask(self, inputs, mask):
    if isinstance(mask, list):
      mask = mask[0]
    if self.return_sequences:
      if not self.merge_mode:
        output_mask = [mask, mask]
      else:
        output_mask = mask
    else:
      output_mask = [None, None] if not self.merge_mode else None

    if self.return_state:
      states = self.forward_layer.states
      state_mask = [None for _ in states]
      if isinstance(output_mask, list):
        return output_mask + state_mask * 2
      return [output_mask] + state_mask * 2
    return output_mask

  @property
  def constraints(self):
    constraints = {}
    if hasattr(self.forward_layer, 'constraints'):
      constraints.update(self.forward_layer.constraints)
      constraints.update(self.backward_layer.constraints)
    return constraints

  def get_config(self):
    config = {'merge_mode': self.merge_mode}
    if self._num_constants:
      config['num_constants'] = self._num_constants

    if hasattr(self, '_backward_layer_config'):
      config['backward_layer'] = self._backward_layer_config
    base_config = super(Bidirectional, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  @classmethod
  def from_config(cls, config, custom_objects=None):
    # Instead of updating the input, create a copy and use that.
    config = copy.deepcopy(config)
    num_constants = config.pop('num_constants', 0)
    # Handle forward layer instantiation (as would parent class).
    from keras.layers import deserialize as deserialize_layer  # pylint: disable=g-import-not-at-top
    config['layer'] = deserialize_layer(
        config['layer'], custom_objects=custom_objects)
    # Handle (optional) backward layer instantiation.
    backward_layer_config = config.pop('backward_layer', None)
    if backward_layer_config is not None:
      backward_layer = deserialize_layer(
          backward_layer_config, custom_objects=custom_objects)
      config['backward_layer'] = backward_layer
    # Instantiate the wrapper, adjust it and return it.
    layer = cls(**config)
    layer._num_constants = num_constants
    return layer

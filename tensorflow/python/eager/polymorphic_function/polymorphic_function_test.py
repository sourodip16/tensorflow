# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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

import collections
import functools
import multiprocessing.pool
import re
import sys
import time
import weakref

from absl.testing import parameterized
import numpy

from tensorflow.python.autograph.core import ag_ctx
from tensorflow.python.autograph.lang import directives
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.data.ops import iterator_ops
from tensorflow.python.eager import backprop
from tensorflow.python.eager import cancellation
from tensorflow.python.eager import context
from tensorflow.python.eager.polymorphic_function import monomorphic_function
from tensorflow.python.eager.polymorphic_function import polymorphic_function
from tensorflow.python.eager.polymorphic_function import tracing_compiler
from tensorflow.python.framework import composite_tensor
from tensorflow.python.framework import config
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import errors
from tensorflow.python.framework import func_graph
from tensorflow.python.framework import function as tf_function
from tensorflow.python.framework import indexed_slices
from tensorflow.python.framework import ops
from tensorflow.python.framework import random_seed
from tensorflow.python.framework import sparse_tensor
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_spec
from tensorflow.python.framework import test_ops
from tensorflow.python.framework import test_util
from tensorflow.python.framework import type_spec
from tensorflow.python.module import module
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import check_ops
from tensorflow.python.ops import clip_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import data_flow_ops
from tensorflow.python.ops import functional_ops
from tensorflow.python.ops import gen_random_ops
from tensorflow.python.ops import gen_sendrecv_ops
from tensorflow.python.ops import gradients_impl
from tensorflow.python.ops import list_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import resource_variable_ops
from tensorflow.python.ops import script_ops
from tensorflow.python.ops import string_ops
from tensorflow.python.ops import variables
from tensorflow.python.ops.ragged import ragged_factory_ops
from tensorflow.python.ops.ragged import ragged_tensor
from tensorflow.python.ops.structured import structured_tensor
from tensorflow.python.platform import test
from tensorflow.python.saved_model.load import load
from tensorflow.python.saved_model.save import save
from tensorflow.python.training import training_ops
from tensorflow.python.util import compat
from tensorflow.python.util import nest
from tensorflow.python.util import tf_decorator


def total_function_cache(defined):
  return defined._list_all_concrete_functions()  # pylint: disable=protected-access


def _example_indexed_slices_with_dense_shape():
  return indexed_slices.IndexedSlices(
      constant_op.constant([1, 2]), constant_op.constant([0, 1]),
      constant_op.constant([2]))


def _example_indexed_slices_without_dense_shape():
  return indexed_slices.IndexedSlices(
      constant_op.constant([1, 2]), constant_op.constant([0, 1]))


def _spec_for_value(value):
  """Returns the (nested) TypeSpec for a value."""
  if nest.is_nested(value):
    return nest.map_structure(_spec_for_value, value)
  elif isinstance(value, (ops.Tensor, composite_tensor.CompositeTensor)):
    return type_spec.type_spec_from_value(value)
  else:
    return value


# This dummy decorator imitates ordinary decorators utilizing tf_decorator.
def dummy_tf_decorator(method):

  def wrapper(*args, **kwargs):
    return method(*args, **kwargs)

  return tf_decorator.make_decorator(method, wrapper)


# TODO(mdan): Organize these tests.
class FunctionTest(test.TestCase, parameterized.TestCase):

  def setUp(self):
    super().setUp()
    cpus = config.list_physical_devices('CPU')
    # Set 4 virtual CPUs
    config.set_logical_device_configuration(cpus[0], [
        context.LogicalDeviceConfiguration(),
        context.LogicalDeviceConfiguration(),
        context.LogicalDeviceConfiguration(),
        context.LogicalDeviceConfiguration()
    ])

  def testBasic(self):
    matmul = polymorphic_function.function(math_ops.matmul)
    t = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])
    sq = matmul(t, t, transpose_a=True)
    sq2 = matmul(sq, t, transpose_a=True)
    self.assertAllEqual(sq.numpy().reshape(-1), [10, 14, 14, 20])
    self.assertAllEqual(sq2.numpy().reshape(-1), [52, 76, 74, 108])

  def testPythonFunctionNotCallable(self):
    with self.assertRaisesRegex(TypeError, 'is not a callable object'):
      polymorphic_function.function(1)

  def testOnExitCallback(self):
    values = []

    def append_1():
      values.append(1)

    def append_2():
      values.append(2)

    def g(x):
      old_values = list(values)
      ops.add_exit_callback_to_default_func_graph(append_1)
      self.assertEqual(old_values, values)
      return x + 1

    tf_g = polymorphic_function.function(g)

    def f(x):
      old_values = list(values)
      ops.add_exit_callback_to_default_func_graph(append_2)
      self.assertEqual(old_values, values)
      return tf_g(x)

    tf_f = polymorphic_function.function(f)
    self.assertEmpty(values)
    tf_f(constant_op.constant(1.0))
    self.assertEqual(values, [1, 2])  # Once for g, once for f.
    tf_f(constant_op.constant([1.0]))  # force a retrace
    self.assertEqual(values, [1, 2, 1, 2])  # And again.

  def testCannotAddExitCallbackWhenNotInFunctionScope(self):
    with self.assertRaisesRegex(RuntimeError, 'when not building a function.'):
      ops.add_exit_callback_to_default_func_graph(lambda: None)

  def testVariable(self):
    v1 = variables.Variable(1.0)
    add = polymorphic_function.function(lambda x, v: x + v1 + v)
    v2 = variables.Variable(1.0)
    x = constant_op.constant(1.0)
    r = add(x, v2)
    self.assertEqual(3.0, self.evaluate(r))

  def testVariableOnly(self):
    v = variables.Variable(1.0)
    add = polymorphic_function.function(lambda x: x.assign_add(1.0))
    r1 = add(v)
    self.assertEqual(2.0, self.evaluate(r1))
    c = constant_op.constant(1.0)
    with self.assertRaisesRegex(AttributeError, 'no attribute'):
      add(c)

  def testVariableMultiFunction(self):

    @polymorphic_function.function
    def second(dup_var, dup_var_2, some_const):
      return dup_var + dup_var_2 + some_const

    @polymorphic_function.function
    def first(dup_var, some_const):
      return second(dup_var, dup_var, some_const)

    my_const = constant_op.constant(1)
    my_var = variables.Variable(2, dtype=dtypes.int32)
    self.assertEqual(second(my_var, my_var, my_const).numpy(), 5)
    self.assertEqual(first(my_var, my_const).numpy(), 5)

  @test_util.disable_tfrt('Packed tensor is not supported in tfrt yet.')
  def testPackedVariable(self):
    with ops.device('/cpu:0'):
      v0_0 = resource_variable_ops.ResourceVariable(1.0)
    with ops.device('/cpu:1'):
      v0_1 = resource_variable_ops.ResourceVariable(2.0)
      v1_0 = resource_variable_ops.ResourceVariable(3.0)
    with ops.device('/cpu:2'):
      v1_1 = resource_variable_ops.ResourceVariable(4.0)

    packed_var_0 = ops.pack_eager_tensors([v0_0.handle, v0_1.handle])
    packed_var_1 = ops.pack_eager_tensors([v1_0.handle, v1_1.handle])

    # TODO(b/145922293): use ResourceVariable.assign_add and
    # ResourceVariable.read_value directly once we support packing multiple
    # ResourceVariable into one ResourceVariable.
    @polymorphic_function.function
    def read_var():
      resource_variable_ops.assign_add_variable_op(packed_var_0,
                                                   constant_op.constant(5.0))
      resource_variable_ops.assign_add_variable_op(packed_var_1,
                                                   constant_op.constant(6.0))
      with ops.device('/cpu:0'):
        read0 = resource_variable_ops.read_variable_op(
            packed_var_0, dtype=dtypes.float32)
      with ops.device('/cpu:1'):
        read1 = resource_variable_ops.read_variable_op(
            packed_var_0, dtype=dtypes.float32)
        read2 = resource_variable_ops.read_variable_op(
            packed_var_1, dtype=dtypes.float32)
      with ops.device('/cpu:2'):
        read3 = resource_variable_ops.read_variable_op(
            packed_var_1, dtype=dtypes.float32)

      return read0, read1, read2, read3

    arg_attrs = read_var.get_concrete_function().function_def.arg_attr
    self.assertLen(arg_attrs, 2)
    self.assertEqual(arg_attrs[0].attr['_composite_device'].s,
                     compat.as_bytes(packed_var_0.device))
    self.assertEqual(arg_attrs[1].attr['_composite_device'].s,
                     compat.as_bytes(packed_var_1.device))

    self.assertAllEqual(read_var(), (1 + 5, 2 + 5, 3 + 6, 4 + 6))

  def testImplementsAttributeBasic(self):
    v = polymorphic_function.function(
        experimental_implements='func')(lambda x, y: x + y)
    with context.graph_mode(), self.cached_session():
      a = array_ops.placeholder(dtypes.float32, ())
      b = array_ops.placeholder(dtypes.float32, ())
      v(a, b)
      gradients_impl.gradients(v(a, b), [a, b])
      fdefs = ops.get_default_graph().as_graph_def().library.function
      self.assertLen(fdefs, 3)
      not_present = 0
      present = 0
      for f in fdefs:
        name = f.signature.name
        if 'forward' in name or 'backward' in name:
          not_present += 1
          self.assertNotIn(monomorphic_function.IMPLEMENTS_ATTRIBUTE_NAME,
                           f.attr, f)
        else:
          present += 1
          self.assertEqual(
              f.attr[monomorphic_function.IMPLEMENTS_ATTRIBUTE_NAME].s,
              'func'.encode('ascii'), f)
      self.assertEqual(not_present, 2, fdefs)
      self.assertEqual(present, 1, fdefs)

  def testImplementsAttributeAssertsOnSideInput(self):
    with context.graph_mode(), self.cached_session():
      z = array_ops.zeros(0)
      v = polymorphic_function.function(
          experimental_implements='func')(lambda x, y: x + y + z)
      a = array_ops.ones((1,))
      b = array_ops.ones((1,))
      with self.assertRaisesRegex(AssertionError,
                                  'variables are always captured'):
        v(a, b)
      functions = ops.get_default_graph().as_graph_def().library.function
      self.assertEmpty(functions)

  def testImplementsAttributeWorksWithGradientTape(self):
    add = lambda x, y: x + y**2
    add = polymorphic_function.function(experimental_implements='MyFunc')(add)
    x = variables.Variable(3.0)
    y = variables.Variable(2.0)

    with backprop.GradientTape() as tape:
      g = add(x, y)

    dg_dy, dg_dx = tape.gradient(g, [y, x])
    self.assertEqual(dg_dy.numpy(), 4.0)
    self.assertEqual(dg_dx.numpy(), 1.0)

  def testImplementsAttributeWorksOnVariables(self):
    with context.graph_mode(), self.cached_session():
      v = polymorphic_function.function(
          experimental_implements='func')(lambda x, y: x + y)
      a = variables.Variable((1.0,))
      b = variables.Variable((1.0,))
      r1 = v(a, b)
      _ = v(a, a)
      functions = ops.get_default_graph().as_graph_def().library.function
      # Verify that we created only one function
      self.assertLen(functions, 1)
      # Verify that eval() reads the current values.
      a.initializer.run()
      b.initializer.run()
      self.assertEqual(r1.eval(), 2)

      a.assign_add([1]).eval()
      self.assertEqual(r1.eval(), 3)

  def testImplementsAttributeWorksOnConstants(self):
    with context.graph_mode(), self.cached_session():
      v = polymorphic_function.function(
          experimental_implements='func')(lambda x, y: x + y)
      a = variables.Variable(1.0)
      r1 = v(a, 2.)
      r2 = v(2., a)
      functions = ops.get_default_graph().as_graph_def().library.function
      self.assertLen(functions, 1)
      self.assertLen(functions[0].signature.input_arg, 2)
      # Verify that eval() reads the current values.
      a.initializer.run()
      self.assertEqual(r1.eval(), 3)
      self.assertEqual(r2.eval(), 3)

  def testImplementsAttributeSpecializes(self):
    with context.graph_mode(), self.cached_session():
      v = polymorphic_function.function(
          experimental_implements='func')(lambda x, y: x + y)
      a = variables.Variable(1.0)
      r1 = v(a, [2.])
      r2 = v([2., 2], a)
      functions = ops.get_default_graph().as_graph_def().library.function
      self.assertLen(functions, 2)
      # Ensure that all parameters are still there and haven't been inlined!

      self.assertLen(functions[0].signature.input_arg, 2)
      self.assertLen(functions[1].signature.input_arg, 2)
      # Verify that eval() reads the current values.
      a.initializer.run()
      numpy.testing.assert_equal(r1.eval(), [3.])
      numpy.testing.assert_equal(r2.eval(), [3., 3.])

  def testImplementsWorksWithTensorSpec(self):
    v = polymorphic_function.function(
        experimental_implements='func')(lambda x, y: x + y)
    v = v.get_concrete_function(
        tensor_spec.TensorSpec(shape=None, dtype=dtypes.float32),
        tensor_spec.TensorSpec(shape=None, dtype=dtypes.float32))
    x = v(1., 2.)
    self.assertEqual(x.numpy(), 3.)

  def testImplementsAttributeAsNameAttrList(self):
    implements_attr = (
        'name: "embedding_matmul" attr {   key: "key1"   value {     i: 2   } '
        '} attr {   key: "key2"   value {     b: false   } }')
    v = polymorphic_function.function(
        experimental_implements=implements_attr)(lambda x, y: x + y)
    with context.graph_mode(), self.cached_session():
      a = array_ops.placeholder(dtypes.float32, ())
      b = array_ops.placeholder(dtypes.float32, ())
      v(a, b)
      gradients_impl.gradients(v(a, b), [a, b])
      fdefs = ops.get_default_graph().as_graph_def().library.function
      self.assertLen(fdefs, 3)
      not_present = 0
      present = 0
      for f in fdefs:
        name = f.signature.name
        if 'forward' in name or 'backward' in name:
          not_present += 1
          self.assertNotIn(monomorphic_function.IMPLEMENTS_ATTRIBUTE_NAME,
                           f.attr, f)
        else:
          present += 1
          attr_value = f.attr[monomorphic_function.IMPLEMENTS_ATTRIBUTE_NAME]
          self.assertIsNotNone(attr_value.func, f)
          self.assertEqual(attr_value.func.name, 'embedding_matmul')
          name_attrs = attr_value.func.attr
          self.assertLen(name_attrs, 2)
      self.assertEqual(not_present, 2, fdefs)
      self.assertEqual(present, 1, fdefs)

  def testReduceTracingWithNestedTFFunction(self):
    v = resource_variable_ops.ResourceVariable([1., 2.])

    @polymorphic_function.function(reduce_retracing=True)
    def inner_test_fn(x):
      x.assign_add([2., 2.])
      return x

    @polymorphic_function.function(reduce_retracing=True)
    def test_fn(x):
      x.assign_add([1., 1.])
      return inner_test_fn(x)

    with backprop.GradientTape() as tape:
      y = test_fn(v)

    grad = tape.gradient(y, v)
    self.assertAllEqual(y, [4., 5.])
    self.assertAllEqual(grad, [1., 1.])

    with backprop.GradientTape() as tape:
      y = test_fn(v)

    grad = tape.gradient(y, v)
    self.assertAllEqual(y, [7., 8.])
    self.assertAllEqual(grad, [1., 1.])

  def testInputShapeRelaxationOnInstanceMethod(self):
    # Test that reduce_retracing is passed during
    # instance method bounding.
    unknown_dim = [False]

    class Foo:

      @polymorphic_function.function(reduce_retracing=True)
      def func(self, a):
        if a._shape_tuple()[0] is None:
          unknown_dim[0] = True
        return a + 1

    foo = Foo()
    foo.func(constant_op.constant([]))
    self.assertFalse(unknown_dim[0])

    foo.func(constant_op.constant([1.0]))
    self.assertTrue(unknown_dim[0])

    foo.func(constant_op.constant([1.0, 2.0]))
    self.assertTrue(unknown_dim[0])

  def testInputShapeFunctionRelaxationWithRaggedTensors(self):
    traced_type_spec = [None]

    @polymorphic_function.function(reduce_retracing=True)
    def func(x):
      traced_type_spec[0] = x._type_spec
      return x

    def check_trace(x, expected_trace):
      traced_type_spec[0] = None
      func(x)
      self.assertEqual(traced_type_spec[0], expected_trace)

    check_trace(  # Initial call gets traced.
        ragged_factory_ops.constant([[1], [2, 3, 4]]),
        ragged_tensor.RaggedTensorSpec([2, None], dtypes.int32))
    check_trace(  # Input TypeSpec is the same -> no retrace.
        ragged_factory_ops.constant([[1, 2], [3, 4]]), None)
    check_trace(  # Even if component tensor shapes change -> no retrace.
        ragged_factory_ops.constant([[1, 2], [3, 4, 5, 6]]), None)
    check_trace(  # Different TypeSpec shape (nrows): relax & retrace
        ragged_factory_ops.constant([[1], [2], [3]]),
        ragged_tensor.RaggedTensorSpec([None, None], dtypes.int32))
    check_trace(  # Different nrows again: relax & retrace
        ragged_factory_ops.constant([[1], [2], [3], [4]]), None)
    check_trace(  # Different nrows yet again: not retrace
        ragged_factory_ops.constant([[1]]), None)
    check_trace(  # Different ragged_rank: retrace
        ragged_factory_ops.constant([[[1]]]),
        ragged_tensor.RaggedTensorSpec([1, None, None], dtypes.int32))
    check_trace(  # Different ragged_rank again: retrace & relax
        ragged_factory_ops.constant([[[1]], [[2]]]),
        ragged_tensor.RaggedTensorSpec([None, None, None], dtypes.int32))

  def testInputShapeFunctionRelaxationWithStructuredTensors(self):
    traced_type_spec = [None]

    @polymorphic_function.function(reduce_retracing=True)
    def func(x):
      traced_type_spec[0] = x._type_spec
      return x

    def check_trace(x, expected_trace):
      traced_type_spec[0] = None
      func(x)
      self.assertEqual(traced_type_spec[0], expected_trace)

    # If we have TypeSpecs that differ in ways other than just their shape,
    # then retrace each time.
    check_trace(
        structured_tensor.StructuredTensor.from_pyval({'a': [1]}),
        structured_tensor.StructuredTensor.Spec._from_fields_and_rank(
            fields={'a': tensor_spec.TensorSpec((1,), dtypes.int32)}, rank=0))
    check_trace(
        structured_tensor.StructuredTensor.from_pyval({'b': [1]}),
        structured_tensor.StructuredTensor.Spec._from_fields_and_rank(
            fields={'b': tensor_spec.TensorSpec((1,), dtypes.int32)}, rank=0))
    check_trace(
        structured_tensor.StructuredTensor.from_pyval({'c': [1]}),
        structured_tensor.StructuredTensor.Spec._from_fields_and_rank(
            fields={'c': tensor_spec.TensorSpec((1,), dtypes.int32)}, rank=0))

    # But if we call again with only shape different, then do relax:
    check_trace(  # relax & retrace
        structured_tensor.StructuredTensor.from_pyval({'a': [1, 2]}),
        structured_tensor.StructuredTensor.Spec._from_fields_and_rank(
            fields={'a': tensor_spec.TensorSpec((None,), dtypes.int32)},
            rank=0))
    check_trace(  # use relaxed graph
        structured_tensor.StructuredTensor.from_pyval({'a': [1, 2, 3]}), None)
    check_trace(  # use relaxed graph
        structured_tensor.StructuredTensor.from_pyval({'a': [1, 2, 3, 4]}),
        None)

  def testInputShapeFunctionRelaxationWithDatasetIterators(self):
    # For dataset iterators, the TypeSpec includes type information that's
    # not derivable from the component tensors.  Make sure that the TypeSpec
    # shapes get relaxed as appropriate.

    traced_type_spec = [None]

    @polymorphic_function.function(reduce_retracing=True)
    def func(x):
      traced_type_spec[0] = x._type_spec
      return x

    def check_trace(x, expected_trace):
      traced_type_spec[0] = None
      func(x)
      self.assertEqual(traced_type_spec[0], expected_trace)

    ds_1_2 = dataset_ops.DatasetV2.from_tensors(array_ops.zeros([1, 2]))
    ds_2_2 = dataset_ops.DatasetV2.from_tensors(array_ops.zeros([2, 2]))
    ds_3_2 = dataset_ops.DatasetV2.from_tensors(array_ops.zeros([3, 2]))
    ds_4_2 = dataset_ops.DatasetV2.from_tensors(array_ops.zeros([4, 2]))
    ds_2_1 = dataset_ops.DatasetV2.from_tensors(array_ops.zeros([2, 1]))
    check_trace(  # shape=[1, 2]: retrace
        dataset_ops.make_one_shot_iterator(ds_1_2),
        iterator_ops.IteratorSpec(
            tensor_spec.TensorSpec([1, 2], dtypes.float32)))
    check_trace(  # shape=[1, 2]: no retrace (use the [1, 2] graph)
        dataset_ops.make_one_shot_iterator(ds_1_2), None)
    check_trace(  # shape=[2, 2]: relax to [None, 2] and retrace
        dataset_ops.make_one_shot_iterator(ds_2_2),
        iterator_ops.IteratorSpec(
            tensor_spec.TensorSpec([None, 2], dtypes.float32)))
    check_trace(  # shape=[3, 2]: no retrace (use the [None, 2] graph)
        dataset_ops.make_one_shot_iterator(ds_3_2), None)
    check_trace(  # shape=[4, 2]: no retrace (use the [None, 2] graph)
        dataset_ops.make_one_shot_iterator(ds_4_2), None)
    check_trace(  # shape=[2, 1]: relax to [None, None] and retrace
        dataset_ops.make_one_shot_iterator(ds_2_1),
        iterator_ops.IteratorSpec(
            tensor_spec.TensorSpec([None, None], dtypes.float32)))

  def testCapturesVariables(self):
    a = variables.Variable(1.0, trainable=False)
    b = variables.Variable(1.0)
    cc = [None]

    @polymorphic_function.function
    def f():
      c = cc[0]
      if c is None:
        c = cc[0] = variables.Variable(1.)
      return a + b + c + 1

    cf = f.get_concrete_function()
    c = cc[0]

    captured_variables = {v.ref() for v in (a, b, c)}
    trainable_variables = {v.ref() for v in (b, c)}
    self.assertEqual({v.ref() for v in cf.variables}, captured_variables)
    self.assertEqual({v.ref() for v in cf.trainable_variables},
                     trainable_variables)
    self.assertEqual(cf.variables, cf.graph.variables)
    self.assertEqual(cf.trainable_variables, cf.graph.trainable_variables)

  def testNestedShapeFunctionRelaxation(self):
    traced_shape = None
    # The inner function will go through shape relaxation because the shapes it
    # receives will be [1], [2], [3], ...
    @polymorphic_function.function(reduce_retracing=True)
    def bar(x_shape):
      nonlocal traced_shape
      traced_shape = x_shape._shape_tuple()
      return x_shape

    # The outer function will not go through shape relaxation because the shapes
    # it receives will be [1], [[1]], [[[1]]], ...
    @polymorphic_function.function(reduce_retracing=True)
    def foo(ones):
      return bar(array_ops.shape(ones))

    self.assertAllEqual(self.evaluate(foo(array_ops.ones([1]))), [1])
    self.assertEqual(traced_shape, (1,))

    for rank in range(2, 6):
      x_shape = self.evaluate(foo(array_ops.ones([1] * rank)))
      self.assertAllEqual(x_shape, [1] * rank)
      self.assertEqual(traced_shape, (None,))

  def testNoHash(self):

    @polymorphic_function.function()
    def f(_):
      return 1.0

    with self.assertRaisesRegex(
        TypeError, r'could not be represented through the generic tracing'):
      f(set([]))

  def testBasicGraphMode(self):
    matmul = polymorphic_function.function(math_ops.matmul)

    @polymorphic_function.function
    def sq(a):
      return matmul(a, a)

    t = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])
    out = sq(t)
    self.assertAllEqual(out, math_ops.matmul(t, t).numpy())

  def testNestedInputsGraphMode(self):
    matmul = polymorphic_function.function(math_ops.matmul)

    pair = collections.namedtuple('pair', ['a', 'b'])

    @polymorphic_function.function
    def a_times_b(inputs):
      return matmul(inputs.a['a'], inputs.b['b'])

    t = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])

    out = a_times_b(pair({'a': t}, {'b': t}))
    self.assertAllEqual(out, math_ops.matmul(t, t).numpy())

  def testNestedOutputsGraphMode(self):
    matmul = polymorphic_function.function(math_ops.matmul)

    pair = collections.namedtuple('pair', ['a', 'b'])

    @polymorphic_function.function()
    def pairs_mul(pair_a, pair_b):
      return pair(matmul(pair_a.a, pair_b.a), matmul(pair_a.b, pair_b.b))

    a = constant_op.constant([[1.0, 2.0], [1.0, 2.0]])
    b = constant_op.constant([[3.0, 4.0], [3.0, 4.0]])

    out = pairs_mul(pair(a, b), pair(b, a))
    expected = pair(
        math_ops.matmul(a, b).numpy(),
        math_ops.matmul(b, a).numpy())
    self.assertAllClose(out, expected)

  def testNestedFunctionGraphNotOutOfDate(self):

    @polymorphic_function.function
    def f():
      return constant_op.constant(1.)

    class _Model(object):

      @polymorphic_function.function
      def g(self):
        self.f = f.get_concrete_function()

    model = _Model()
    model.g()
    concrete = model.f
    weak_g_graph = weakref.ref(model.g.get_concrete_function().graph)
    self.assertIs(weak_g_graph(), concrete.graph.outer_graph)
    weak_g = weakref.ref(model.g)
    del model
    self.assertIsNone(weak_g())
    self.assertIsNone(weak_g_graph())
    self.assertIsNotNone(concrete.graph.outer_graph)
    self.assertIs(ops.get_default_graph(), concrete.graph.outer_graph)

  def testBasicGraphFunction(self):
    matmul = polymorphic_function.function(math_ops.matmul)

    @polymorphic_function.function
    def sq(a):
      return matmul(a, a)

    t = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])

    sq_op = sq.get_concrete_function(t)
    self.assertEqual(sq_op.output_shapes, tensor_shape.TensorShape([2, 2]))
    out = sq_op(t)
    self.assertAllEqual(out, math_ops.matmul(t, t).numpy())

  def testGetConcreteFunctionThreadSafety(self):

    @polymorphic_function.function
    def sq():
      t = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])
      return math_ops.matmul(t, t)

    concrete_functions = []

    def thread_func(_):
      cf = sq.get_concrete_function()
      concrete_functions.append(cf)

    num_threads = 100
    pool = multiprocessing.pool.ThreadPool(num_threads)
    _ = pool.map(thread_func, list(range(num_threads)))

    self.assertLen(set(concrete_functions), 1)

  def testGetConcreteFunctionThreadSafetyWithArgs(self):

    @polymorphic_function.function
    def add_100(*args):
      return math_ops.add_n(args)

    p = multiprocessing.pool.ThreadPool(2)
    args = (constant_op.constant(1.),) * 100
    f1, f2 = p.map(add_100.get_concrete_function, [args] * 2)
    # I see about len(args) + max(0, len(args) - 3) arguments expected.
    f1(*args)
    del f2

  def testInputSpecGraphFunction(self):
    matmul = polymorphic_function.function(math_ops.matmul)

    @polymorphic_function.function
    def sq(a):
      return matmul(a, a)

    sq_op = sq.get_concrete_function(
        tensor_spec.TensorSpec((None, None), dtypes.float32))
    self.assertEqual([None, None], sq_op.output_shapes.as_list())

    t1 = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])
    out1 = sq_op(t1)
    self.assertAllEqual(out1, math_ops.matmul(t1, t1).numpy())

    t2 = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])
    out2 = sq_op(t2)
    self.assertAllEqual(out2, math_ops.matmul(t2, t2).numpy())

  def testNestedInputSpecGraphFunction(self):
    matmul = polymorphic_function.function(math_ops.matmul)

    @polymorphic_function.function
    def sq(mats):
      ((a, b),) = mats
      return matmul(a, b)

    sq_op_autonamed = sq.get_concrete_function([(tensor_spec.TensorSpec(
        (None, None),
        dtypes.float32), tensor_spec.TensorSpec((None, None), dtypes.float32))])
    self.assertEqual([None, None], sq_op_autonamed.output_shapes.as_list())

    sq_op = sq.get_concrete_function([(tensor_spec.TensorSpec((None, None),
                                                              dtypes.float32,
                                                              name='first_mat'),
                                       tensor_spec.TensorSpec(
                                           (None, None),
                                           dtypes.float32,
                                           name='second_mat'))])
    self.assertEqual([None, None], sq_op.output_shapes.as_list())

    t1 = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])
    t2 = constant_op.constant([[1.4, 2.4], [3.4, 4.4]])
    out = sq_op(first_mat=t1, second_mat=t2)
    self.assertAllEqual(out, math_ops.matmul(t1, t2).numpy())
    self.assertAllEqual(
        sq_op_autonamed(t1, t2),
        math_ops.matmul(t1, t2).numpy())

  def testExecutingStatelessDefunConcurrently(self):

    @polymorphic_function.function
    def stateless(x):
      return math_ops.multiply(2.0, x)

    pool = multiprocessing.pool.ThreadPool()
    inputs = [constant_op.constant(1.0 * x) for x in range(100)]
    outputs = [float(out) for out in pool.map(stateless, inputs)]
    expected = [float(2.0 * x) for x in inputs]
    self.assertSequenceEqual(outputs, expected)

  def testExecutingManyStatelessDefunsConcurrently(self):

    @polymorphic_function.function
    def stateless(x):
      del x
      return math_ops.multiply(2.0, 2.0)

    pool = multiprocessing.pool.ThreadPool()
    # `pool.map` below instantiates 100 functions, one for each object.
    objects = [object() for _ in range(100)]
    outputs = [float(out) for out in pool.map(stateless, objects)]
    expected = [4.0] * 100
    self.assertSequenceEqual(outputs, expected)

  @test_util.disable_tfrt('b/169431085: This test is flaky on tfrt')
  def testExecutingStatefulDefunConcurrently(self):

    v = resource_variable_ops.ResourceVariable(1.0)

    @polymorphic_function.function
    def stateful(x):
      v.assign(x)

    pool = multiprocessing.pool.ThreadPool()
    inputs = [constant_op.constant(0.0)] * 100
    pool.map(stateful, inputs)
    self.assertEqual(float(v.read_value()), 0.0)

  def testExecutingManyStatefulDefunsConcurrently(self):

    v = resource_variable_ops.ResourceVariable(1.0)

    @polymorphic_function.function
    def stateful(x):
      del x
      return v.assign(0.0)

    pool = multiprocessing.pool.ThreadPool()
    # `pool.map` below instantiates 100 functions, one for each object.
    pool.map(stateful, [object() for _ in range(100)])
    self.assertEqual(float(v.read_value()), 0.0)

  def testShareRendezvous(self):

    # Disable grappler from inlining the functions. Note we run the send & recv
    # in graph mode since with eager mode the function should automatically be
    # inlined.
    context.context().set_optimizer_experimental_options(
        {'disable_meta_optimizer': True})

    cpu = '/device:CPU:0'

    signature = [tensor_spec.TensorSpec([], dtypes.int32)]

    @polymorphic_function.function
    def send():
      x = constant_op.constant(1)
      gen_sendrecv_ops.send(x, 'x', cpu, 0, cpu)
      return x

    send._shared_rendezvous = True  # pylint: disable=protected-access

    @polymorphic_function.function(input_signature=signature)
    def send_body(n):
      send()
      return n - 1

    @polymorphic_function.function
    def recv():
      return gen_sendrecv_ops.recv(dtypes.int32, 'x', cpu, 0, cpu)

    recv._shared_rendezvous = True  # pylint: disable=protected-access

    @polymorphic_function.function(input_signature=signature)
    def recv_body(n):
      recv()
      return n - 1

    @polymorphic_function.function(input_signature=signature)
    def cond(n):
      return n > 0

    # Instead of calling the send & recv functions directly we want to call them
    # through a functional while to ensure the rendezvous is shared across the
    # while boundary.
    @polymorphic_function.function
    def fn(n):
      functional_ops.While([n], cond.get_concrete_function(),
                           send_body.get_concrete_function())
      return functional_ops.While([n], cond.get_concrete_function(),
                                  recv_body.get_concrete_function())

    # Use a graph context since functions will not be automatically inlined
    with context.graph_mode(), self.cached_session():
      self.evaluate(fn(2))

  def disabled_testRandomSeed(self):

    @polymorphic_function.function
    def f():
      return random_ops.random_normal(())

    random_seed.set_random_seed(1)
    x = f()
    self.assertNotEqual(x, f())
    random_seed.set_random_seed(1)
    self.assertAllEqual(f(), x)

  def testNestedInputsGraphFunction(self):
    matmul = polymorphic_function.function(math_ops.matmul)

    pair = collections.namedtuple('pair', ['a', 'b'])

    @polymorphic_function.function
    def a_times_b(inputs):
      return matmul(inputs.a['a'], inputs.b['b'])

    t = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])
    sq_op = a_times_b.get_concrete_function(
        pair(
            dict(a=tensor_spec.TensorSpec([2, 2], dtypes.float32, 'a')),
            dict(b=tensor_spec.TensorSpec([2, 2], dtypes.float32, 'b'))))
    self.assertEqual(sq_op.output_shapes, tensor_shape.TensorShape([2, 2]))
    out = sq_op(a=t, b=t)
    self.assertAllEqual(out, math_ops.matmul(t, t).numpy())

  def testNestedOutputGraphFunction(self):
    matmul = polymorphic_function.function(math_ops.matmul)

    @polymorphic_function.function
    def sq(a):
      return (matmul(a, a), {'b': constant_op.constant(1.0)})

    t = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])

    sq_op = sq.get_concrete_function(t)
    self.assertEqual(sq_op.output_shapes, (tensor_shape.TensorShape([2, 2]), {
        'b': tensor_shape.TensorShape([])
    }))
    self.assertEqual(sq_op.output_dtypes, (dtypes.float32, {
        'b': dtypes.float32
    }))
    (a, b) = sq_op(t)
    self.assertAllEqual(a, math_ops.matmul(t, t).numpy())
    self.assertAllEqual(b['b'].numpy(), 1.0)

  def testGraphFunctionNoneOutput(self):

    @polymorphic_function.function
    def fn(unused_a, unused_b):
      return None

    x = constant_op.constant(1)
    fn_op = fn.get_concrete_function(x, x)
    self.assertEqual(fn_op.output_dtypes, None)
    self.assertEqual(fn_op.output_shapes, None)
    self.assertAllEqual(fn_op(x, x), None)

  def testDefunCapturedInt32(self):
    x = constant_op.constant(1, dtype=dtypes.int32)

    @polymorphic_function.function
    def add_int32s():
      return x + x

    self.assertEqual(2, int(add_int32s()))

  def testDefunReadVariable(self):
    v = resource_variable_ops.ResourceVariable(1.0)

    @polymorphic_function.function
    def f():
      return v.read_value()

    self.assertEqual(1.0, float(f()))

  def testDefunAssignAddVariable(self):
    v = resource_variable_ops.ResourceVariable(1.0)
    x = constant_op.constant(2.0)

    @polymorphic_function.function
    def test_assign_add():
      v.assign_add(x)
      return v.read_value()

    self.assertEqual(3.0, float(test_assign_add()))

  @test_util.run_in_graph_and_eager_modes
  def testTensorInitializationInFunctionRaisesError(self):

    @polymorphic_function.function
    def tensor_init():
      with self.assertRaisesRegex(ValueError, 'could not be lifted out'):
        resource_variable_ops.ResourceVariable(constant_op.constant(2.0))

    tensor_init()

  @test_util.run_in_graph_and_eager_modes
  def testCallableTensorInitializationInFunction(self):

    @polymorphic_function.function
    def tensor_init():
      self.v = resource_variable_ops.ResourceVariable(
          lambda: constant_op.constant(2.0))
      return self.v.read_value()

    value = tensor_init()
    if not context.executing_eagerly():
      self.evaluate(variables.global_variables_initializer())
    self.assertEqual(self.evaluate(value), 2.0)

  @test_util.also_run_as_tf_function
  def testInitScopeTensorInitializationInFunction(self):

    @polymorphic_function.function
    def tensor_init():
      with ops.init_scope():
        const = constant_op.constant(2.0)
      # Note: this variable bypasses tf.function's variable creation
      # requirements by bypassing variable_creator_scope by using
      # ResourceVariable instead of Variable.
      self.v = resource_variable_ops.ResourceVariable(const)
      return self.v.read_value()

    value = tensor_init()
    self.assertAllEqual(value, 2.0)

  @test_util.run_in_graph_and_eager_modes
  def testGetConcreteFunctionCreatesVariables(self):

    v_holder = []

    @polymorphic_function.function
    def tensor_init():
      if not v_holder:
        v_holder.append(variables.Variable(5.))
      return v_holder[0].read_value()

    concrete = tensor_init.get_concrete_function()
    self.evaluate(variables.global_variables_initializer())
    self.assertAllEqual(5., self.evaluate(concrete()))
    self.assertAllEqual(5., self.evaluate(tensor_init()))

  def testFuncGraphCaptureByValue(self):
    v = variables.Variable(1.0)

    def trivial_function():
      return v.read_value()

    graph_function = tracing_compiler.TracingCompiler(
        trivial_function, 'test', capture_by_value=True)

    self.assertAllEqual(graph_function(), 1.0)
    v.assign(2.0)
    self.assertAllEqual(graph_function(), 1.0)

  def testFuncGraphCaptureByValueNested(self):
    v = variables.Variable(1.0)

    def trivial_function():
      return control_flow_ops.cond(
          array_ops.placeholder_with_default(True, ()), v.read_value,
          v.read_value)

    graph_function = tracing_compiler.TracingCompiler(
        trivial_function, 'test', capture_by_value=True)

    self.assertAllEqual(graph_function(), 1.0)
    v.assign(2.0)
    self.assertAllEqual(graph_function(), 1.0)

  def testDefunShapeInferenceWithCapturedResourceVariable(self):
    v = resource_variable_ops.ResourceVariable([[1, 2], [3, 4]])

    def f():
      x = constant_op.constant([[1, 2], [3, 4]])
      out = math_ops.matmul(v, x)
      self.assertEqual(out.shape, tensor_shape.TensorShape([2, 2]))
      # We do not return v directly since the tensor conversion function of
      # ResourceVariable returns the read value and not the resource itself.
      return v._handle

    compiled = polymorphic_function.function(f)
    var_handle = compiled()
    self.assertEqual(var_handle.dtype, dtypes.resource)
    self.assertEqual(var_handle.shape, tensor_shape.TensorShape([]))
    var_t = resource_variable_ops.read_variable_op(var_handle, dtype=v.dtype)
    self.assertEqual(var_t.shape, tensor_shape.TensorShape([2, 2]))

  def testShapeInferenceForMoreSpecificInput(self):

    def f(a):
      return array_ops.reshape(a, [-1, 3])

    signature = [tensor_spec.TensorSpec(None, dtypes.float32)]
    compiled = polymorphic_function.function(f, input_signature=signature)

    @polymorphic_function.function
    def use_f():
      inputs = array_ops.zeros([10, 10, 3])
      self.assertAllEqual(f(inputs).shape, compiled(inputs).shape)

    use_f()

  def testDefunShapeInferenceWithCapturedResourceVariableInGraphMode(self):
    with context.graph_mode():
      v = resource_variable_ops.ResourceVariable([[1, 2], [3, 4]])

      def f():
        x = constant_op.constant([[1, 2], [3, 4]])
        out = math_ops.matmul(v, x)
        self.assertEqual(out.shape, tensor_shape.TensorShape([2, 2]))
        # We do not return v directly since the tensor conversion function of
        # ResourceVariable returns the read value and not the resource itself.
        return v._handle

      compiled = polymorphic_function.function(f)
      var_handle = compiled()
      self.assertEqual(var_handle.dtype, dtypes.resource)
      self.assertEqual(var_handle.shape, tensor_shape.TensorShape([]))
      var_t = resource_variable_ops.read_variable_op(var_handle, dtype=v.dtype)
      self.assertEqual(var_t.shape, tensor_shape.TensorShape([2, 2]))

  def testDefunShapeInferenceWithCapturedVariableInGraphMode(self):
    with context.graph_mode():
      v = variables.Variable([[1, 2], [3, 4]])

      def f():
        x = constant_op.constant([[1, 2], [3, 4]])
        out = math_ops.matmul(v, x)
        self.assertEqual(out.shape, tensor_shape.TensorShape([2, 2]))

      # Check that shape inference works while creating the defun
      compiled = polymorphic_function.function(f)
      compiled()

  def testDefunShapeInferenceWithCapturedTensorListInGraphMode(self):
    with context.graph_mode():
      tensor_list = list_ops.empty_tensor_list(
          element_dtype=dtypes.float32,
          element_shape=ops.convert_to_tensor([], dtype=dtypes.int32))
      tensor_list = list_ops.tensor_list_push_back(tensor_list,
                                                   constant_op.constant(1.0))
      tensor_list = list_ops.tensor_list_push_back(tensor_list,
                                                   constant_op.constant(2.0))

      def f():
        tl, value = list_ops.tensor_list_pop_back(
            tensor_list, element_dtype=dtypes.float32)
        self.assertEqual(value.shape, tensor_shape.TensorShape([]))
        return tl

      compiled = polymorphic_function.function(f)
      output_tensor_list = compiled()
      _, value = list_ops.tensor_list_pop_back(
          output_tensor_list, element_dtype=dtypes.float32)
      self.assertEqual(value.shape, tensor_shape.TensorShape([]))

  def testRunMetadata(self):

    @polymorphic_function.function
    def f(x):
      return x * x

    with ops.device('cpu:0'):
      context.enable_run_metadata()
      f(constant_op.constant(1.0))
    run_metadata = context.export_run_metadata()
    context.disable_run_metadata()
    self.assertLen(run_metadata.partition_graphs, 1)

  def testGraphModeCaptureVariable(self):
    with context.graph_mode(), self.cached_session():

      class HasAVar:

        def __init__(self):
          self.v = resource_variable_ops.ResourceVariable(1.0)

        def call(self):
          return self.v * 2

      o = HasAVar()
      self.evaluate(variables.global_variables_initializer())
      call = polymorphic_function.function(o.call)
      op = call()
      self.assertAllEqual(self.evaluate(op), 2.0)

  def testGraphModeManyFunctions(self):
    with ops.Graph().as_default(), self.cached_session():

      @polymorphic_function.function
      def f(x):
        return x * x

      @polymorphic_function.function
      def g(x):
        return f(x) + 1

      self.assertAllEqual(g(constant_op.constant(2.0)), 5.0)

  def testDict(self):

    @polymorphic_function.function
    def f(x):
      return {'name': x + 1}

    self.assertAllEqual(f(constant_op.constant(1.0))['name'], 2.0)

  def testWeakrefInputsRejected(self):

    @polymorphic_function.function
    def f(x):
      return x

    class Dummy:
      pass

    o = Dummy()
    wr = weakref.ref(o)

    with self.assertRaisesRegex(ValueError, 'weakref'):
      f(wr)

  def testTensorConversionWithDefun(self):

    @polymorphic_function.function
    def f(x):
      return math_ops.add(x, constant_op.constant(3))

    self.assertAllEqual(5, f(constant_op.constant(2)))

  def testTensorConversionCall(self):

    @polymorphic_function.function
    def f(x):
      return math_ops.add(x, constant_op.constant(3))

    @polymorphic_function.function
    def g(x):
      return f(f(x))

    self.assertAllEqual(8, g(constant_op.constant(2)))

  def testCallShape(self):

    @polymorphic_function.function
    def f(x):
      return x + 1

    @polymorphic_function.function
    def g(x):
      x = f(x)
      self.assertEqual(x.shape.as_list(), [])
      return None

    g(constant_op.constant(1.0))

  def testNestedDefunWithNoOutputAndTapedInput(self):
    three = resource_variable_ops.ResourceVariable(3.0, name='v')

    @polymorphic_function.function
    def f(x):
      # This function intentionally takes a taped variable as input,
      # but does not return any values
      math_ops.add(x, three)

    @polymorphic_function.function
    def g(x):
      y = math_ops.add(x, three)
      f(y)

    g(three)

  def testGatherResourceWithDefun(self):
    with ops.device('cpu:0'):
      v = resource_variable_ops.ResourceVariable([0.0, 1.0, 2.0])

    def sum_gather():
      return math_ops.reduce_sum(array_ops.gather(v, [1, 2]))

    defined = polymorphic_function.function(sum_gather)
    self.assertAllEqual(sum_gather(), defined())

  @parameterized.named_parameters([
      ('IndexedSlicesWithDenseShape',
       _example_indexed_slices_with_dense_shape,),
      ('IndexedSlicesWithoutDenseShape',
       _example_indexed_slices_without_dense_shape,),
      ('RaggedTensorRaggedRank1', ragged_tensor.RaggedTensor.from_row_lengths,
       {'values': [1, 2, 3], 'row_lengths': [2, 0, 1]}),
      ('RaggedTensorRaggedRank2',
       ragged_tensor.RaggedTensor.from_nested_row_lengths,
       {'flat_values': [1, 2, 3], 'nested_row_lengths': [[1, 2], [2, 0, 1]]}),
      ('SparseTensor', sparse_tensor.SparseTensor,
       {'values': [1, 2, 3], 'indices': [[0], [8], [10]], 'dense_shape': [20]}),
  ])  # pyformat: disable
  def testReturnCompositeTensorWithDefun(self,
                                         factory_fn,
                                         factory_kwargs={},
                                         input_signature=None):
    input_ct = factory_fn(**factory_kwargs)

    @polymorphic_function.function(input_signature=input_signature)
    def f():
      return input_ct

    output_ct = f()
    self.assertIsInstance(output_ct, type(input_ct))
    nest.assert_same_structure(input_ct, output_ct, expand_composites=True)

    input_flat = nest.flatten(input_ct, expand_composites=True)
    output_flat = nest.flatten(output_ct, expand_composites=True)
    for (input_component, output_component) in zip(input_flat, output_flat):
      self.assertAllEqual(input_component, output_component)

  @parameterized.named_parameters([
      ('IndexedSlicesWithDenseShape',
       _example_indexed_slices_with_dense_shape,),
      ('IndexedSlicesWithoutDenseShape',
       _example_indexed_slices_without_dense_shape,),
      ('RaggedTensorRaggedRank1',
       ragged_tensor.RaggedTensor.from_row_lengths,
       {'values': [1, 2, 3], 'row_lengths': [2, 0, 1]}),
      ('RaggedTensorRaggedRank2',
       ragged_tensor.RaggedTensor.from_nested_row_lengths,
       {'flat_values': [1, 2, 3], 'nested_row_lengths': [[1, 2], [2, 0, 1]]}),
      ('SparseTensor',
       sparse_tensor.SparseTensor,
       {'values': [1, 2, 3], 'indices': [[0], [8], [10]], 'dense_shape': [20]}),
      ('RaggedTensorRaggedRank1WithSignature',
       ragged_tensor.RaggedTensor.from_row_lengths,
       {'values': [1, 2, 3], 'row_lengths': [2, 0, 1]},
       [ragged_tensor.RaggedTensorSpec([None, None], dtypes.int32)]),
      ('RaggedTensorRaggedRank2WithSignature',
       ragged_tensor.RaggedTensor.from_nested_row_lengths,
       {'flat_values': [1, 2, 3], 'nested_row_lengths': [[1, 2], [2, 0, 1]]},
       [ragged_tensor.RaggedTensorSpec([None, None, None], dtypes.int32)]),
      ('SparseTensorWithSignature',
       sparse_tensor.SparseTensor,
       {'values': [1, 2, 3], 'indices': [[0], [8], [10]], 'dense_shape': [20]},
       [sparse_tensor.SparseTensorSpec([None], dtypes.int32)]),
  ])  # pyformat: disable
  def testCompositeAsArgumentTensorWithDefun(self,
                                             factory_fn,
                                             factory_kwargs={},
                                             input_signature=None):
    input_ct = factory_fn(**factory_kwargs)

    @polymorphic_function.function(input_signature=input_signature)
    def f(x):
      return x

    output_ct = f(input_ct)
    self.assertIsInstance(output_ct, type(input_ct))
    nest.assert_same_structure(input_ct, output_ct, expand_composites=True)

    input_flat = nest.flatten(input_ct, expand_composites=True)
    output_flat = nest.flatten(output_ct, expand_composites=True)
    for (input_component, output_component) in zip(input_flat, output_flat):
      self.assertAllEqual(input_component, output_component)

  def testTracedCompositeDiscardsShapeInfo(self):
    # SparseTensorSpec intentionally excludes info about the number of elements
    # that are in a sparse tensor (which is recorded as st.indices.shape[0] and
    # st.values.shape[0]).  Similarly, RaggedTensorSpec intentionally excludes
    # info about the total number of values in a RaggedTensor (stored as
    # rt.values.shape[0]).  This test checks that the placeholders created by
    # tf.function() properly mask this shape info.
    @polymorphic_function.function
    def f(rt, st):
      self.assertEqual(st.indices.shape.as_list()[:1], [None])
      self.assertEqual(st.values.shape.as_list(), [None])
      return (rt, st)

    rt = ragged_factory_ops.constant([[1, 2], [3]])
    st = sparse_tensor.SparseTensor([[0]], [0], [10])
    f(rt, st)

  @test_util.run_gpu_only
  def testFunctionOnDevice(self):
    x = constant_op.constant([1.]).gpu()
    f = polymorphic_function.function(math_ops.add)
    y = f(x, x).cpu()
    self.assertAllEqual(y, [2.])

  @test_util.run_gpu_only
  @test_util.run_in_graph_and_eager_modes
  def testOpInFunctionWithConflictingResourceInputs(self):
    with ops.device('/cpu:0'):
      v_cpu = resource_variable_ops.ResourceVariable([0.0, 1.0, 2.0],
                                                     name='cpu')
      v_also_cpu = resource_variable_ops.ResourceVariable([0.0, 1.0, 2.0],
                                                          name='also_cpu')

    with ops.device('/gpu:0'):
      v_gpu = resource_variable_ops.ResourceVariable([0.0, 1.0, 2.0],
                                                     name='gpu')

    @polymorphic_function.function
    def resource_apply_adam():
      training_ops.resource_apply_adam(
          v_cpu.handle,
          v_gpu.handle,
          v_also_cpu.handle,
          1.0,  # beta1_power
          1.0,  # beta2_power
          1.0,  # learning_rate
          1.0,  # beta1
          1.0,  # beta2
          1.0,  # epsilon,
          [1.0, 1.0, 1.0],  # grad
          False)  # use_locking
      return None

    with self.assertRaisesRegex(
        errors.InvalidArgumentError,
        'Cannot place the graph because a reference or resource edge connects '
        'colocation groups with incompatible assigned devices'):
      if not context.executing_eagerly():
        self.evaluate(variables.global_variables_initializer())
      self.evaluate(resource_apply_adam())

  @test_util.run_gpu_only
  def testFunctionHandlesInputsOnDifferentDevices(self):
    # The Reshape op requires the shape tensor to be placed in host memory.
    reshape = polymorphic_function.function(array_ops.reshape)
    value = constant_op.constant([1., 2.]).gpu()
    shape = constant_op.constant([2, 1])
    reshaped = reshape(value, shape).cpu()
    self.assertAllEqual(reshaped, [[1], [2]])

  @test_util.run_gpu_only
  def testFunctionHandlesInputsPlacedOnTheWrongDeviceGracefully(self):
    # The Reshape op requires the shape tensor to be placed in host memory.
    reshape = polymorphic_function.function(array_ops.reshape)
    value = constant_op.constant([1., 2.])
    shape = constant_op.constant([2, 1]).gpu()
    reshape(value, shape)  # No error is raised

  def testNoneOutput(self):

    @polymorphic_function.function
    def my_function(_):
      return None

    self.assertAllEqual(my_function(1), None)

  def testNestedFunctions(self):
    # TensorFlow function (which is what would be used in TensorFlow graph
    # construction).
    @tf_function.Defun(dtypes.int32, dtypes.int32)
    def add(a, b):
      return math_ops.add(a, b)

    @polymorphic_function.function
    def add_one(x):
      return add(x, 1)

    self.assertAllEqual(3, add_one(constant_op.constant(2)))

  def testVariableCaptureInNestedFunctions(self):
    v = resource_variable_ops.ResourceVariable(1, dtype=dtypes.int32)

    @polymorphic_function.function
    def inner_read():
      return v.read_value()

    @polymorphic_function.function
    def outer():
      return inner_read()

    self.assertEqual(1, int(outer()))

  def testReturnCapturedEagerTensor(self):
    t = constant_op.constant(1)

    @polymorphic_function.function
    def read():
      return t

    self.assertEqual(1, int(read()))

  def testReturnCapturedGraphTensor(self):
    with context.graph_mode(), self.cached_session():
      t = constant_op.constant(1)

      @polymorphic_function.function
      def read():
        return t

      self.assertEqual(1, int(self.evaluate(read())))

  def testSequenceInputs(self):
    clip_by_global_norm = polymorphic_function.function(
        clip_ops.clip_by_global_norm)
    t_list = [constant_op.constant(1.0), constant_op.constant(2.0)]
    clipped_list, global_norm = clip_by_global_norm(t_list,
                                                    constant_op.constant(.2))
    for t in clipped_list:
      self.assertIsInstance(t, ops.Tensor)
    self.assertIsInstance(global_norm, ops.Tensor)

  def testNestedSequenceInputs(self):

    def my_op(inputs):
      a, b, c = inputs
      e, f = b
      g, h = e
      return [a + a, [tuple([f + f, g + g]), h + h], c + c], a + f + g + h + c

    my_eager_op = polymorphic_function.function(my_op)
    ret = my_eager_op([
        constant_op.constant(1),
        [(constant_op.constant(2), constant_op.constant(3)),
         constant_op.constant(4)],
        constant_op.constant(5)
    ])
    self.assertLen(ret, 2)
    self.assertAllEqual(ret[0][0], 2)
    self.assertAllEqual(ret[0][1][0][0], 8)
    self.assertAllEqual(ret[0][1][0][1], 4)
    self.assertIsInstance(ret[0][1][0], tuple)
    self.assertAllEqual(ret[0][1][1], 6)
    self.assertAllEqual(ret[0][2], 10)
    self.assertAllEqual(ret[1], 15)

  def testVariableNamesRespectNameScopesWithDefun(self):

    @polymorphic_function.function
    def create_variable():
      with ops.name_scope('foo', skip_on_eager=False):
        v = resource_variable_ops.ResourceVariable(0.0, name='bar')
      self.assertEqual(v.name, 'foo/bar:0')

    create_variable()

  def testVariableNamesRespectNameScopesWithDefunInGraph(self):
    with context.graph_mode():

      @polymorphic_function.function
      def create_variable():
        with ops.name_scope('foo', skip_on_eager=False):
          v = resource_variable_ops.ResourceVariable([1.0, 2.0], name='bar')
        self.assertEqual(v.name, 'foo/bar:0')

      with ops.get_default_graph().as_default():
        create_variable()

  @test_util.run_in_graph_and_eager_modes
  def testVariablesPlacedOnOutsideDevice(self):

    class _Obj(object):

      def __init__(self):
        self.v = None

      @polymorphic_function.function
      def f(self):
        if self.v is None:
          self.v = variables.Variable(1.)
        return self.v + 1.

    has_device = _Obj()
    with ops.device('cpu:0'):
      has_device.f()
    self.assertIn('CPU', has_device.v.device)

  @test_util.run_in_graph_and_eager_modes
  def testCallingGraphFunctionOnDifferentDevice(self):

    def func():
      return constant_op.constant(0)

    defined = polymorphic_function.function(func)
    with ops.device('cpu:0'):
      cpu_graph_function = defined.get_concrete_function()

    with ops.device('cpu:0'):
      self.assertEqual(
          self.evaluate(cpu_graph_function()), self.evaluate(func()))

    with ops.device('cpu:1'):
      self.assertEqual(0., self.evaluate(cpu_graph_function()))

    with ops.device(None):
      self.assertEqual(0., self.evaluate(cpu_graph_function()))

    default_graph_function = defined.get_concrete_function()
    self.assertEqual(
        self.evaluate(default_graph_function()), self.evaluate(func()))

    with ops.device('cpu:1'):
      self.assertEqual(0., self.evaluate(default_graph_function()))

  @test_util.run_gpu_only
  @test_util.run_in_graph_and_eager_modes
  def testColocateWithRespected(self):
    # TODO(b/113291792): Use multiple CPUs instead of a GPU.
    with ops.device('cpu:0'):
      x = array_ops.identity(1.0)

    with ops.device('gpu:0'):
      y = array_ops.identity(1.0)

    @polymorphic_function.function
    def foo():
      return test_ops.device_placement_op()

    with ops.colocate_with(x):
      self.assertIn(compat.as_bytes('CPU:0'), self.evaluate(foo()))

    with ops.colocate_with(y):
      self.assertIn(compat.as_bytes('GPU:0'), self.evaluate(foo()))

  def testVariablesAreTracked(self):
    v = resource_variable_ops.ResourceVariable(1.0)

    def foo(x):
      return v * x

    defined = polymorphic_function.function(foo)

    x = constant_op.constant([1.0])
    self.assertEqual(1., self.evaluate(defined(x)))
    v.assign(2.)

    x = constant_op.constant([1.0, 2.0])
    self.assertAllEqual([2., 4.], self.evaluate(defined(x)))

  def testInputSignatureMustBeSequenceOfTensorSpecs(self):

    def foo(a, b):
      del a
      del b

    # Signatures must consist exclusively of `TensorSpec` objects.
    signature = [(2, 3), tensor_spec.TensorSpec([2, 3], dtypes.float32)]
    with self.assertRaisesRegex(TypeError, 'input_signature.*nested sequence'):
      polymorphic_function.function(foo, input_signature=signature)

  @test_util.run_in_graph_and_eager_modes
  def testInputsIncompatibleWithSignatureRaisesError(self):

    def foo(a):
      return a

    signature = [tensor_spec.TensorSpec(shape=(2,), dtype=dtypes.float32)]
    defined = polymorphic_function.function(foo, input_signature=signature)

    # Invalid shapes.
    with self.assertRaisesRegex(ValueError, 'Python inputs incompatible.*'):
      defined(array_ops.ones([3]))

    with self.assertRaisesRegex(ValueError, 'Python inputs incompatible.*'):
      defined(array_ops.ones([2, 1]))

    # Wrong number of arguments.
    with self.assertRaisesRegex(TypeError, 'specifies 1 .* got 2'):
      defined(array_ops.ones([2]), array_ops.ones([2]))
    with self.assertRaisesRegex(ValueError,
                                'Structure of Python function inputs.*'):
      defined()

    with self.assertRaisesRegex(ValueError,
                                'inputs incompatible with input_signature'):
      defined.get_concrete_function(
          tensor_spec.TensorSpec(shape=(3,), dtype=dtypes.float32))

  def testMismatchedConcreteSignatureRaisesError(self):

    @polymorphic_function.function
    def run_test():

      @polymorphic_function.function
      def f(x):
        return x

      with self.assertRaisesRegex(
          TypeError, 'ConcreteFunction .* was constructed .* but was called'):
        f.get_concrete_function(1)(constant_op.constant(1))

      with self.assertRaisesRegex(TypeError, r'f\(x\) expected .* but got .*'):
        f.get_concrete_function(constant_op.constant(1))(1)

      with self.assertRaisesRegex(
          TypeError, 'ConcreteFunction .* was constructed .* but was called'):
        f.get_concrete_function(1)(2)

    run_test()

  def testInputSignatureConversionWithDefaultArg(self):

    def foo(a, training=True):
      if training:
        return a
      else:
        return -1.0 * a

    signature = [
        tensor_spec.TensorSpec([], dtypes.float32),
        tensor_spec.TensorSpec([], dtypes.bool),
    ]
    defined = polymorphic_function.function(foo, input_signature=signature)
    a = constant_op.constant(1.0)
    self.assertAllEqual(a.numpy(), defined(a))
    self.assertAllEqual(a.numpy(), defined(a, training=True))
    self.assertAllEqual(-a.numpy(), defined(a, training=False))

  def testVariableSpecWithInputSignature(self):

    def f(v):
      v.assign_add(1)

    signature = [
        resource_variable_ops.VariableSpec(shape=[], dtype=dtypes.int32)
    ]
    with self.assertRaisesRegex(TypeError,
                                "input_signature doesn't support VariableSpec"):
      polymorphic_function.function(f, input_signature=signature)

  def testDefuningInstanceMethod(self):

    integer = constant_op.constant(2, dtypes.int64)

    class Foo:

      def one(self, tensor):
        return tensor

      @polymorphic_function.function
      def two(self, tensor, other=integer):
        return self.one(tensor), other

    foo = Foo()
    t = constant_op.constant(1.0)
    one, two = foo.two(t)
    self.assertEqual(one.numpy(), 1.0)
    self.assertEqual(two.numpy(), 2)

  def testDefuningInstanceMethodWithDefaultArgument(self):

    integer = constant_op.constant(2, dtypes.int64)

    class Foo:

      @polymorphic_function.function
      def func(self, other=integer):
        return other

    foo = Foo()
    self.assertEqual(foo.func().numpy(), int(integer))

  def testPythonCallWithSideEffects(self):
    state = []

    @polymorphic_function.function
    def side_effecting_function():
      state.append(0)

    side_effecting_function()
    self.assertAllEqual(state, [0])

    # The second invocation should call the graph function, which shouldn't
    # trigger the list append.
    side_effecting_function()
    self.assertAllEqual(state, [0])

    # Whereas calling the python function directly should create a side-effect.
    side_effecting_function.python_function()
    self.assertAllEqual(state, [0, 0])

  def testFunctionWithNestedFunctionCallAndSideEffects(self):
    v1 = variables.Variable(1.0)
    v2 = variables.Variable(1.0)

    @polymorphic_function.function
    def add_one(a):
      a.assign_add(1.0)

    # Grappler will inline calls to `add_one` into the function body, we check
    # that all side-effects were executed.
    @polymorphic_function.function
    def side_effecting_function(a, b):
      add_one(a)
      add_one(b)
      return a + b

    result = side_effecting_function(v1, v2)
    self.assertEqual(result.numpy(), 4.0)

  def testRegisterConcreteFunction(self):

    @polymorphic_function.function
    def py_add(x, y):
      return math_ops.add(x, y)

    py_add(array_ops.ones([]), array_ops.ones([]))
    add = py_add.get_concrete_function(
        tensor_spec.TensorSpec(None, dtypes.float32),
        tensor_spec.TensorSpec(None, dtypes.float32))

    @polymorphic_function.function
    def py_composite(x, y):
      return x, add(x, y)

    py_composite(array_ops.ones([]), array_ops.ones([]))
    composite = py_composite.get_concrete_function(
        tensor_spec.TensorSpec(None, dtypes.float32),
        tensor_spec.TensorSpec(None, dtypes.float32))

    with context.graph_mode(), self.cached_session():
      with ops.get_default_graph().as_default():
        t = constant_op.constant([[1.0, 2.0], [3.0, 4.0]])
        composite.add_to_graph()
        composite.add_gradient_functions_to_graph()

        graph = ops.get_default_graph()
        # pylint: disable=protected-access
        self.assertLen(graph._functions, 6)
        # two sets of functions, each of them are (inference, forward, backward)
        functions = list(graph._functions.values())
        captured_function_names = [
            f.definition.signature.name for f in functions
        ]
        expected_func_name_regex = [
            '.*inference.*py_composite.*',
            '.*inference.*py_add.*',
            '.*forward.*py_composite.*',
            '.*forward.*py_add.*',
            '.*inference.*backward.*py_composite.*',
            '.*inference.*backward.*py_add.*',
        ]
        for expected, found in zip(expected_func_name_regex,
                                   captured_function_names):
          self.assertRegex(found, expected)

        composite_t, composite_double = composite(t, t)
        double = add(t, t)
        self.assertAllEqual([[2, 4], [6, 8]], self.evaluate(double))
        self.assertAllEqual([[2, 4], [6, 8]], self.evaluate(composite_double))
        self.assertAllEqual([[1, 2], [3, 4]], self.evaluate(composite_t))
        # Make sure the pre registered function is used, and no other function
        # is added.
        self.assertLen(graph._functions, 6)

  def testEagerCaptures(self):
    with context.eager_mode():
      large_tensor = array_ops.ones(shape=(256,))
      self.assertGreater(256, func_graph._EAGER_CONST_THRESHOLD)

      small_tensor = array_ops.ones(shape=(4,))
      self.assertLessEqual(4, func_graph._EAGER_CONST_THRESHOLD)

      v = resource_variable_ops.ResourceVariable(0.0)

    for captured, op_type in [(large_tensor, 'Placeholder'),
                              (small_tensor, 'Const'), (v, 'Placeholder')]:

      @polymorphic_function.function
      def test_fn():
        return captured + 1  # pylint: disable=cell-var-from-loop

      g = test_fn.get_concrete_function().graph
      internal_captures = g.internal_captures
      self.assertLen(internal_captures, 1)
      self.assertEqual(internal_captures[0].op.type, op_type)

  def testVariableAliasIdInStructuredInputSignature(self):

    @polymorphic_function.function
    def foo(v1, v2):
      return v1 + v2

    v1 = resource_variable_ops.ResourceVariable(1.0)
    v2 = resource_variable_ops.ResourceVariable(2.0)
    graph_function = foo.get_concrete_function(v1, v1)
    args_sig, _ = graph_function.graph.structured_input_signature
    expected_spec = resource_variable_ops.VariableSpec([], alias_id=0)
    self.assertLen(args_sig, 2)
    self.assertEqual(args_sig[0], expected_spec)
    self.assertEqual(args_sig[1], expected_spec)

    graph_function = foo.get_concrete_function(v1, v2)
    args_sig, _ = graph_function.graph.structured_input_signature
    expected_spec1 = resource_variable_ops.VariableSpec([], alias_id=0)
    expected_spec2 = resource_variable_ops.VariableSpec([], alias_id=1)
    self.assertLen(args_sig, 2)
    self.assertEqual(args_sig[0], expected_spec1)
    self.assertEqual(args_sig[1], expected_spec2)

  def testStructuredSignatureAndMultipleVariables(self):
    self.skipTest('b/209081027: Enable this test after Variable becomes a '
                  'CompositeTensor and Variable gets expand to handle tensor.')

    @polymorphic_function.function
    def foo(v1, v2):
      return v1 + v2

    v1 = resource_variable_ops.ResourceVariable(1.0)
    v2 = resource_variable_ops.ResourceVariable(2.0)
    graph_function = foo.get_concrete_function(v1, v1)
    self.assertAllEqual(graph_function(v1, v1), 2.0)
    with self.assertRaises(TypeError):
      graph_function(v1, v2)

  def _total_function_cache_def_func(self, defined):
    return defined._list_all_concrete_functions()  # pylint: disable=protected-access

  def testVariableRetracingOnDtypeChanges(self):

    @polymorphic_function.function
    def defined(a, b):
      return a + b

    x1 = resource_variable_ops.ResourceVariable(0.0)
    x2 = resource_variable_ops.ResourceVariable(0.0)

    defined(x1, x2)
    self.assertLen(self._total_function_cache_def_func(defined), 1)

    # Should expect retracing for new dtypes
    y1 = resource_variable_ops.ResourceVariable(0)
    y2 = resource_variable_ops.ResourceVariable(1)
    defined(y1, y2)
    self.assertLen(self._total_function_cache_def_func(defined), 2)

  def testVariableRetracingDtypeShape(self):

    @polymorphic_function.function
    def defined(a, b):
      return a + b

    x1 = resource_variable_ops.ResourceVariable(0.0)
    x2 = resource_variable_ops.ResourceVariable(0.0)

    defined(x1, x2)
    self.assertLen(self._total_function_cache_def_func(defined), 1)

    y1 = resource_variable_ops.ResourceVariable([0.0, 1.0])
    y2 = resource_variable_ops.ResourceVariable([0.0, 1.0])

    defined(y1, y2)
    self.assertLen(self._total_function_cache_def_func(defined), 2)

    z1 = resource_variable_ops.ResourceVariable([[0.0, 1.0]])
    z2 = resource_variable_ops.ResourceVariable([[0.0, 1.0]])
    defined(z1, z2)
    self.assertLen(self._total_function_cache_def_func(defined), 3)

  def testFunctionModifiesInputList(self):
    # Tests on `list` methods that do in place modification, except `list.sort`
    # since it cannot even be "defunned" in the first place

    def get_list():
      return [constant_op.constant(0.), constant_op.constant(1.)]

    expected_msg = '.*() should not modify'

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def append(l):
        l.append(constant_op.constant(0.))

      append(get_list())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def extend(l):
        l.extend([constant_op.constant(0.)])

      extend(get_list())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def insert(l):
        l.insert(0, constant_op.constant(0.))

      insert(get_list())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def pop(l):
        l.pop()

      pop(get_list())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def reverse(l):
        l.reverse()

      reverse(get_list())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def remove(l):
        l.remove(l[0])

      remove(get_list())

    # `list.clear` is a method that is in Py3 but not Py2
    if sys.version.startswith('3'):

      with self.assertRaisesRegex(ValueError, expected_msg):

        @polymorphic_function.function
        def clear(l):
          l.clear()

        clear(get_list())

    # One last test for keyword arguments
    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def kwdappend(**kwargs):
        l = kwargs['l']
        l.append(constant_op.constant(0.))

      kwdappend(l=get_list())

  def testFunctionModifiesInputDict(self):

    def get_dict():
      return {'t1': constant_op.constant(0.), 't2': constant_op.constant(1.)}

    expected_msg = '.* should not modify'

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def clear(m):
        m.clear()

      clear(get_dict())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def pop(m):
        m.pop('t1')

      pop(get_dict())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def popitem(m):
        m.popitem()

      popitem(get_dict())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def update(m):
        m.update({'t1': constant_op.constant(3.)})

      update(get_dict())

    with self.assertRaisesRegex(ValueError, expected_msg):

      @polymorphic_function.function
      def setdefault(m):
        m.setdefault('t3', constant_op.constant(3.))

      setdefault(get_dict())

  def testFunctionModifiesInputNest(self):
    with self.assertRaisesRegex(ValueError, 'modify.* should not modify'):

      @polymorphic_function.function
      def modify(n):
        n[0]['t1'].append(constant_op.constant(1.))

      nested_input = [{
          't1': [constant_op.constant(0.),
                 constant_op.constant(1.)],
      },
                      constant_op.constant(2.)]

      modify(nested_input)

    with self.assertRaisesRegex(ValueError,
                                'modify_same_flat.* should not modify'):

      # The flat list doesn't change whereas the true structure changes
      @polymorphic_function.function
      def modify_same_flat(n):
        n[0].append(n[1].pop(0))

      nested_input = [[constant_op.constant(0.)],
                      [constant_op.constant(1.),
                       constant_op.constant(2.)]]

      modify_same_flat(nested_input)

  def testFunctionStackInErrorMessage(self):
    if context.executing_eagerly():
      # TODO(b/122736651): Remove this skipTest once fixed.
      self.skipTest('Error interpolation is not working when function is '
                    'invoked without PartitionedCallOp.')

    @polymorphic_function.function()
    def fn3(x):
      return x + 2

    @polymorphic_function.function()
    def fn2(x):
      check_ops.assert_equal(fn3(x), 3)
      return 2

    @polymorphic_function.function()
    def fn(x):
      return fn2(x)

    with self.assertRaises(errors.InvalidArgumentError) as cm:
      fn(2)
    e = cm.exception
    self.assertIn('fn -> fn2', e.message)
    self.assertIn('node assert_equal/Assert/Assert (defined at', e.message)
    self.assertNotIn('fn3', e.message)

  @test_util.run_gpu_only
  def testFunctionIsNotPinned(self):
    """Tests that functions aren't pinned to the CPU by the eager runtime."""
    seed1, seed2 = 79, 25
    shape = constant_op.constant([4, 7])
    dtype = dtypes.float32

    @polymorphic_function.function
    def func():
      with ops.device('GPU:0'):
        return gen_random_ops.random_standard_normal(
            shape, dtype=dtype, seed=seed1, seed2=seed2)

    with ops.device('GPU:0'):
      x = func()
      self.assertRegex(x.device, 'GPU')

  def testLimitedRetracingWithCompositeTensors(self):
    trace_count = [0]

    @polymorphic_function.function
    def f(x):
      trace_count[0] += 1
      return x

    for i in range(10):
      f(ragged_factory_ops.constant([[1, 2], [i]]))
      f(ragged_factory_ops.constant([[1, 2], [], [3, 4, 5]]))
      f(ragged_factory_ops.constant([[[1, 2], [3]], [[4, 5, 6]]]))
      self.assertEqual(trace_count[0], 3)

  def test_concrete_function_shape_mismatch(self):

    @polymorphic_function.function
    def f(argument_name):
      return argument_name + 1.

    f_concrete = f.get_concrete_function(constant_op.constant([1.]))

    # Calling a function from eager doesn't do any shape checking above what
    # kernels do while executing.
    self.assertAllEqual([2., 3.],
                        f_concrete(constant_op.constant([1., 2.])).numpy())

    @polymorphic_function.function
    def g():
      f_concrete(constant_op.constant([1., 2.]))

    with self.assertRaisesRegex(ValueError, 'is not compatible with the shape'):
      g()

  @test_util.run_in_graph_and_eager_modes
  def test_shape_inference_with_symbolic_shapes(self):

    @polymorphic_function.function
    def _uses_symbolic_shapes(w, x, y):
      x = array_ops.identity(x, name='name_collision')
      x = array_ops.transpose(x, [1, 0, 2])
      x_batch = array_ops.shape(x)[0]
      y_batch = array_ops.shape(y)[0]
      y *= w
      n = y_batch // x_batch
      return array_ops.reshape(y, [n, x_batch, -1])

    conc = _uses_symbolic_shapes.get_concrete_function(
        tensor_spec.TensorSpec(None, dtypes.float32),
        tensor_spec.TensorSpec(None, dtypes.float32),
        tensor_spec.TensorSpec(None, dtypes.float32))

    @polymorphic_function.function
    def _call_concrete():
      c = constant_op.constant(1.)
      array_ops.identity(c, name='name_collision')
      output1 = conc(
          array_ops.ones([2]), array_ops.ones([5, 4, 2]),
          array_ops.ones([20, 2]))
      self.assertEqual([5, 4, 2], output1.shape)
      output2 = conc(
          array_ops.ones([3]), array_ops.ones([5, 4, 3]),
          array_ops.ones([40, 3]))
      self.assertEqual([10, 4, 3], output2.shape)
      return output1, output2

    output1, output2 = _call_concrete()
    self.assertEqual((5, 4, 2), self.evaluate(output1).shape)
    self.assertEqual((10, 4, 3), self.evaluate(output2).shape)

  def testAutoGraphContext(self):

    @polymorphic_function.function
    def test_fn():
      self.assertEqual(ag_ctx.control_status_ctx().status,
                       ag_ctx.Status.ENABLED)

    prev_status = ag_ctx.control_status_ctx().status
    test_fn()
    self.assertEqual(ag_ctx.control_status_ctx().status, prev_status)

  @test_util.disable_tfrt('b/170435618')
  def testCancelBeforeFunctionExecution(self):
    if not context.executing_eagerly():
      self.skipTest('eager only')

    q = data_flow_ops.FIFOQueue(1, dtypes.int32)

    @polymorphic_function.function
    def f():
      return q.dequeue()

    c_mgr = cancellation.CancellationManager()
    cancelable_func = c_mgr.get_cancelable_function(f.get_concrete_function())

    c_mgr.start_cancel()
    with self.assertRaises(errors.CancelledError):
      cancelable_func()

  @test_util.disable_tfrt('b/170435618')
  def testCancelBlockedFunctionExecution(self):
    if not context.executing_eagerly():
      self.skipTest('eager only')

    q = data_flow_ops.FIFOQueue(1, dtypes.int32)

    @polymorphic_function.function
    def f():
      return q.dequeue()

    c_mgr = cancellation.CancellationManager()
    cancelable_func = c_mgr.get_cancelable_function(f.get_concrete_function())

    def cancel_thread():
      time.sleep(0.5)
      c_mgr.start_cancel()

    t = self.checkedThread(cancel_thread)
    t.start()
    with self.assertRaises(errors.CancelledError):
      cancelable_func()
    t.join()

  @test_util.disable_tfrt('b/170435618')
  def testCancelAfterFunctionExecution(self):
    if not context.executing_eagerly():
      self.skipTest('eager only')

    q = data_flow_ops.FIFOQueue(1, dtypes.int32)
    q.enqueue(37)

    @polymorphic_function.function
    def f():
      return q.dequeue()

    c_mgr = cancellation.CancellationManager()
    cancelable_func = c_mgr.get_cancelable_function(f.get_concrete_function())

    self.assertAllEqual(37, cancelable_func().numpy())

    # Cancellation after the function executes is a no-op.
    c_mgr.start_cancel()

  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionWithNestedTensorInputs(self):

    @polymorphic_function.function
    def f(x, y):
      return (x['a'] + x['b'], y[0] + y[1])

    a = constant_op.constant(1000)
    b = constant_op.constant(200)
    c = constant_op.constant(30)
    d = {'a': a, 'b': b}
    e = (c, 4)

    # Test different argument signatures when constructing the concrete func.
    for cf in [
        f.get_concrete_function(d, e),
        f.get_concrete_function(d, y=e),
        f.get_concrete_function(y=e, x=d),
        f.get_concrete_function(_spec_for_value(d), _spec_for_value(e)),
        f.get_concrete_function(_spec_for_value(d), y=_spec_for_value(e)),
        f.get_concrete_function(y=_spec_for_value(e), x=_spec_for_value(d))
    ]:
      # Test different calling conventions when calling the concrete func.
      for output in [
          cf(d, e),  # structured signature
          cf(d, y=e),  # structured signature w/ kwarg
          cf(y=e, x=d),  # structured signature w/ 2 kwargs
          cf(a, b, c),  # flat signature
      ]:
        self.assertIsInstance(output, tuple)
        self.assertLen(output, 2)
        self.assertAllEqual(output[0], 1200)
        self.assertAllEqual(output[1], 34)

  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionWithNestedNonTensorInputs(self):

    @polymorphic_function.function
    def f(x, y):
      return (x['a'] + x['b'], y[0] + y[1])

    a = {'a': constant_op.constant(1000), 'b': constant_op.constant(200)}
    b = (50, 3)

    for cf in [  # argument y is bound to non-Tensor value (50, 3).
        f.get_concrete_function(a, b),
        f.get_concrete_function(a, y=b),
        f.get_concrete_function(x=a, y=b)
    ]:
      for output in [cf(a), cf(x=a), cf(a, b), cf(x=a, y=b)]:
        self.assertAllEqual(output[0] + output[1], 1253)

  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionWithNonTensorStringInputs(self):

    @polymorphic_function.function
    def f(x, y):
      return string_ops.string_join([x, y])

    a = constant_op.constant('a')
    b = 'b'

    cf = f.get_concrete_function(a, b)
    for output in [cf(a), cf(x=a), cf(a, b), cf(x=a, y=b)]:
      self.assertAllEqual(output, b'ab')

  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionWithBoundNestedNonTensorInputs(self):

    @polymorphic_function.function
    def f(x, y):
      return (x['a'] + x['b'], y[0] + y[1])

    a = {'a': 3000, 'b': 200, 'c': 9000}
    b = (constant_op.constant(30), 4)

    for cf in [  # argument x is bound to non-tensor value `a`
        f.get_concrete_function(a, b),
        f.get_concrete_function(a, y=b),
        f.get_concrete_function(x=a, y=b)
    ]:
      for output in [cf(a, b), cf(a, y=b), cf(y=b), cf(x=a, y=b)]:
        self.assertAllEqual(output[0] + output[1], 3234)

  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionWithAllBoundNestedNonTensorInputs(self):

    @polymorphic_function.function
    def f(x, y):
      return (x['a'] + x['b'], y[0] + y[1])

    a = {'a': 5000, 'b': 500}
    b = (50, 5)

    cf = f.get_concrete_function(a, b)
    for output in [cf(), cf(a), cf(y=b)]:
      self.assertAllEqual(output[0] + output[1], 5555)

  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionMethodWithVarargs(self):
    float32_scalar = tensor_spec.TensorSpec(shape=(), dtype=dtypes.float32)

    class MyModel(module.Module):

      @polymorphic_function.function(
          input_signature=[float32_scalar, float32_scalar])
      def add(self, *arg):
        return math_ops.add(*arg)

    m = MyModel()
    cf = m.add.get_concrete_function()
    cf(-12.0, 3.0)

  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionStructuredSignatureKeywordOrder(self):
    # Check that keyword-only arguments are sorted appropriately, so that they
    # feed the right tensor into each input.
    @polymorphic_function.function
    def g(**kwargs):
      return string_ops.reduce_join(
          string_ops.reduce_join(
              ops.convert_to_tensor(sorted(kwargs.items())),
              axis=1,
              separator='='),
          axis=0,
          separator=', ')

    s = constant_op.constant('s')
    g.get_concrete_function(q=s, a=s, p=s, r=s, v=s, m=s, l=s)
    self.assertAllEqual(
        g(m='a', r='b', v='c', q='d', l='e', a='f', p='g'),
        b'a=f, l=e, m=a, p=g, q=d, r=b, v=c')
    self.assertAllEqual(
        g(q='d', a='f', p='g', r='b', v='c', m='a', l='e'),
        b'a=f, l=e, m=a, p=g, q=d, r=b, v=c')
    self.assertAllEqual(
        g(a='f', l='e', m='a', p='g', q='d', r='b', v='c'),
        b'a=f, l=e, m=a, p=g, q=d, r=b, v=c')

  # pylint: disable=g-long-lambda
  @parameterized.named_parameters([
      dict(
          testcase_name='MissingArg',
          conc_args=lambda: (1, constant_op.constant(2)),
          call_args=lambda: (1,),
          error=r'func\(x, y\) missing required arguments: y'),
      dict(
          testcase_name='MissingVararg',
          conc_args=lambda: (1, 2, constant_op.constant(1.0)),
          call_args=lambda: (1, 2),
          error=r'func\(x, y, <arg3>\) missing required arguments: <arg3>'),
      dict(
          testcase_name='ExtraPositionalArg',
          conc_args=lambda: (1, 2),
          call_args=lambda: (1, 2, 3),
          error=r'func\(x, y\) takes 2 .* got 3'),
      dict(
          testcase_name='MissingKeywordOnlyArg',
          conc_args=lambda: (1, 2),
          conc_kwargs=lambda: {'c': constant_op.constant(1.0)},
          call_args=lambda: (1, 2),
          error=r'func\(x, y, \*, c\) missing required arguments: c'),
      dict(
          testcase_name='ExtraKeywordArg',
          conc_args=lambda: (1, 2),
          call_args=lambda: (1, 2),
          call_kwargs=lambda: {'c': constant_op.constant(1.0)},
          error=r'func\(x, y\) got unexpected keyword arguments: c'),
      dict(
          testcase_name='ExpectedRaggedGotNest',
          conc_args=lambda: (ragged_factory_ops.constant([[1, 2], [3]]),),
          call_args=lambda: ({
              'a': constant_op.constant([1, 2, 3])
          },),
          error=r'func\(x, y\): argument x had incorrect type\n'
          r'  expected: RaggedTensor\n'
          r"       got: {'a': (Eager)?Tensor}"),
      dict(
          testcase_name='WrongRaggedRank',
          conc_args=lambda: (ragged_factory_ops.constant([[1, 2], [3]]),),
          call_args=lambda: (ragged_factory_ops.constant([[[1]]]),),
          error=r'func\(x, y\): argument x had incorrect type\n'),
      dict(
          testcase_name='WrongRaggedDType',
          conc_args=lambda: (ragged_factory_ops.constant([[1]]),),
          call_args=lambda: (ragged_factory_ops.constant([[1.0]]),),
          error=r'func\(x, y\): argument x had incorrect type\n'),
      dict(
          testcase_name='ExpectedDictGotTensor',
          conc_args=lambda: ({
              'a': constant_op.constant(1),
              'b': constant_op.constant(1)
          },),
          call_args=lambda: (constant_op.constant(1),),
          error=r'func\(x, y\): argument x had incorrect type\n'),
      dict(
          testcase_name='ExpectedTupleGotTensor',
          conc_args=lambda:
          ((constant_op.constant(1), constant_op.constant(2)),),
          call_args=lambda: (constant_op.constant(1),),
          error=r'func\(x, y\): argument x had incorrect type\n'),
      dict(
          testcase_name='WrongDType',
          conc_args=lambda: (constant_op.constant(1),),
          call_args=lambda: (constant_op.constant(1.0),),
          exception=(
              ValueError,
              errors.InvalidArgumentError,
              # on xla_gpu, we get InternalError instead.
              errors.InternalError)),
      dict(
          testcase_name='ExpectedTensorGotInt',
          conc_args=lambda: (constant_op.constant(1),),
          call_args=lambda: (5,),
          error=r'func\(x, y\) expected a Tensor in x, but got int value 5'),
      dict(
          testcase_name='ExpectedIntGotDifferentInt',
          conc_args=lambda: (5,),
          call_args=lambda: (8,),
          error=r'ConcreteFunction func\(x, y\) was constructed with int '
          r'value 5 in x, but was called with int value 8'),
      dict(
          testcase_name='ExpectedIntGotTensor',
          conc_args=lambda: (5,),
          call_args=lambda: (constant_op.constant(6),),
          error=r'ConcreteFunction func\(x, y\) was constructed with int '
          'value 5 in x, but was called with (Eager)?Tensor value .*'),
      dict(
          testcase_name='TwoValuesForArgument',
          conc_args=lambda: (1, 2),
          call_args=lambda: (1, 2),
          call_kwargs=lambda: {'x': 3},
          error=r"func\(x, y\) got two values for 'x'"),
  ])
  # pylint: enable=g-long-lambda
  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionStructuredSignatureError(self,
                                                   conc_args=(),
                                                   conc_kwargs=None,
                                                   call_args=(),
                                                   call_kwargs=None,
                                                   error='.*',
                                                   exception=TypeError):
    """Tests for errors in the structrued signature.

    Args:
      conc_args: Positional arguments used for get_concrete_function.
      conc_kwargs: Keyword arguments used for get_concrete_function.
      call_args: Positional arguments used to call the function.
      call_kwargs: Keyword arguments used to call the function.
      error: Expected exception message.
      exception: Expected exception type.
    """
    conc_args = conc_args() if callable(conc_args) else conc_args
    conc_kwargs = conc_kwargs() if callable(conc_kwargs) else conc_kwargs or {}
    call_args = call_args() if callable(call_args) else call_args
    call_kwargs = call_kwargs() if callable(call_kwargs) else call_kwargs or {}
    self.assertIsInstance(conc_args, tuple)
    self.assertIsInstance(call_args, tuple)
    self.assertIsInstance(conc_kwargs, dict)
    self.assertIsInstance(call_kwargs, dict)

    @polymorphic_function.function
    def func(x, y=5, *varargs, **kwargs):  # pylint: disable=keyword-arg-before-vararg
      del y, varargs, kwargs
      return x

    conc = func.get_concrete_function(*conc_args, **conc_kwargs)
    with self.assertRaisesRegex(exception, error):
      self.evaluate(conc(*call_args, **call_kwargs))

  # pylint: disable=g-long-lambda
  @parameterized.named_parameters([
      dict(
          testcase_name='MissingArg',
          conc_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          call_args=lambda: (constant_op.constant(1),),
          error=r'func\(x, y\) missing required arguments: y'),
      dict(
          testcase_name='TwoValuesForArg',
          conc_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          call_args=lambda: (constant_op.constant(1),),
          call_kwargs=lambda: {
              'x': constant_op.constant(1),
              'y': constant_op.constant(1)
          },
          error=r"func\(x, y\) got two values for 'x'"),
      dict(
          testcase_name='ExtraPositionalArg',
          conc_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          call_args=lambda: (constant_op.constant(1), constant_op.constant(2),
                             constant_op.constant(3)),
          error=r'func\(x, y\) takes 2 .* got 3'),
      dict(
          testcase_name='UnexpectedKeywordArg',
          conc_args=lambda: (constant_op.constant(1),),
          call_args=lambda: (constant_op.constant(1),),
          call_kwargs=lambda: {'c': constant_op.constant(1)},
          error=r'func\(x\) got unexpected keyword arguments: c'),
      dict(
          testcase_name='MissingVararg',
          conc_args=lambda: (constant_op.constant(1), constant_op.constant(2),
                             constant_op.constant(3)),
          call_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          error=r'func\(x, y, varargs_0\) missing required '
          r'arguments: varargs_0'),
      dict(
          testcase_name='MissingKeywordArg',
          conc_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          conc_kwargs=lambda: {'c': constant_op.constant(1)},
          call_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          error=r'func\(x, y, c\) missing required arguments: c'),
      dict(
          testcase_name='ExpectedTensorGotInt',
          conc_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          call_args=lambda: (5, constant_op.constant(2)),
          error=r'func\(x, y\): expected argument #0\(zero-based\) to be '
          r'a Tensor; got int \(5\)'),
      dict(
          testcase_name='WrongDType',
          conc_args=lambda: (constant_op.constant(1),),
          call_args=lambda: (constant_op.constant(1.0),),
          exception=(
              ValueError,
              errors.InvalidArgumentError,
              # on xla_gpu, we get InternalError instead.
              errors.InternalError)),
      dict(
          testcase_name='MissingKeywordArgNestPiece',
          conc_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          conc_kwargs=lambda: {'c': ragged_factory_ops.constant([[1]])},
          call_args=lambda: (constant_op.constant(1), constant_op.constant(2)),
          call_kwargs=lambda: {'c': constant_op.constant(1)},
          error=r'func\(x, y, c, c_1\) missing required arguments: c_1'),
  ])
  # pylint: enable=g-long-lambda
  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionFlatSignatureError(self,
                                             conc_args=(),
                                             conc_kwargs=None,
                                             call_args=(),
                                             call_kwargs=None,
                                             error='.*',
                                             exception=TypeError):
    """Tests for errors in the flat signature.

    Args:
      conc_args: Positional arguments used for get_concrete_function.
      conc_kwargs: Keyword arguments used for get_concrete_function.
      call_args: Positional arguments used to call the function.
      call_kwargs: Keyword arguments used to call the function.
      error: Expected exception message.
      exception: Expected exception type.
    """
    conc_args = conc_args() if callable(conc_args) else conc_args
    conc_kwargs = conc_kwargs() if callable(conc_kwargs) else conc_kwargs or {}
    call_args = call_args() if callable(call_args) else call_args
    call_kwargs = call_kwargs() if callable(call_kwargs) else call_kwargs or {}
    self.assertIsInstance(conc_args, tuple)
    self.assertIsInstance(call_args, tuple)
    self.assertIsInstance(conc_kwargs, dict)
    self.assertIsInstance(call_kwargs, dict)

    @polymorphic_function.function
    def func(x, y=5, *varargs, **kwargs):  # pylint: disable=keyword-arg-before-vararg
      del y, varargs, kwargs
      return x

    conc = func.get_concrete_function(*conc_args, **conc_kwargs)

    # Remove _function_spec, to disable the structured signature.
    conc._set_function_spec(None)  # pylint: disable=protected-access

    with self.assertRaisesRegex(exception, error):
      self.evaluate(conc(*call_args, **call_kwargs))

  @test_util.run_in_graph_and_eager_modes
  def testConcreteFunctionAmbiguousSignature(self):
    # When both the flat & structured signatures are applicable, but they
    # give different results, we use the structured signature.  Note: we expect
    # this to be extremely rare.
    @polymorphic_function.function
    def f(x, y):
      return x * 10 + y

    conc = f.get_concrete_function(
        x=tensor_spec.TensorSpec(None, dtypes.int32, name='y'),
        y=tensor_spec.TensorSpec(None, dtypes.int32, name='x'))

    result = conc(x=constant_op.constant(5), y=constant_op.constant(6))
    self.assertAllEqual(result, 56)

  def testPrettyPrintedSignature(self):

    @polymorphic_function.function
    def func(x, kangaroo=None, octopus=7):
      del octopus, kangaroo
      return x

    scalar = constant_op.constant(5)
    vector = constant_op.constant([10, 10, 20])
    ragged = ragged_factory_ops.constant([[10, 20], [40]])

    c1 = func.get_concrete_function(scalar, vector)
    c1_summary = r'func\(x, kangaroo, octopus=7\)'
    c1_details = (r'  Args:\n'
                  r'    x: int32 Tensor, shape=\(\)\n'
                  r'    kangaroo: int32 Tensor, shape=\(3,\)\n'
                  r'  Returns:\n'
                  r'    int32 Tensor, shape=\(\)')
    self.assertRegex(c1.pretty_printed_signature(verbose=False), c1_summary)
    self.assertRegex(
        c1.pretty_printed_signature(verbose=True),
        c1_summary + '\n' + c1_details)
    self.assertRegex(
        repr(c1), r'<ConcreteFunction func\(x, kangaroo, octopus=7\) at .*>')
    self.assertRegex(
        str(c1), 'ConcreteFunction {}\n{}'.format(c1_summary, c1_details))

    c2 = func.get_concrete_function(scalar, ragged, 3)
    c2_summary = r'func\(x, kangaroo, octopus=3\)'
    c2_details = (r'  Args:\n'
                  r'    x: int32 Tensor, shape=\(\)\n'
                  r'    kangaroo: RaggedTensorSpec\(.*\)\n'
                  r'  Returns:\n'
                  r'    int32 Tensor, shape=\(\)')
    self.assertRegex(c2.pretty_printed_signature(),
                     c2_summary + '\n' + c2_details)

    c3 = func.get_concrete_function({'a': scalar, 'b': [ragged, ragged]})
    c3_summary = r'func\(x, kangaroo=None, octopus=7\)'
    c3_details = (r'  Args:\n'
                  r"    x: {'a': <1>, 'b': \[<2>, <3>\]}\n"
                  r'      <1>: int32 Tensor, shape=\(\)\n'
                  r'      <2>: RaggedTensorSpec\(.*\)\n'
                  r'      <3>: RaggedTensorSpec\(.*\)\n'
                  r'  Returns:\n'
                  r"    {'a': <1>, 'b': \[<2>, <3>\]}\n"
                  r'      <1>: int32 Tensor, shape=\(\)\n'
                  r'      <2>: RaggedTensorSpec\(.*\)\n'
                  r'      <3>: RaggedTensorSpec\(.*\)')

    # python 3.5 does not gurantee deterministic iteration of dict contents
    # which can lead mismatch on pretty_printed_signature output for "Args"
    if sys.version_info >= (3, 6):
      self.assertRegex(c3.pretty_printed_signature(),
                       c3_summary + '\n' + c3_details)

    # pylint: disable=keyword-arg-before-vararg
    @polymorphic_function.function
    def func2(x, y=3, *args, **kwargs):
      return (x, y, args, kwargs)

    c4 = func2.get_concrete_function(scalar, 4, 5, a=scalar)
    c4_summary = 'func2(x, y=4, <arg3>=5, *, a)'
    self.assertEqual(c4.pretty_printed_signature(verbose=False), c4_summary)

    c5 = func2.get_concrete_function(8, vector)
    c5_summary = 'func2(x=8, y)'
    self.assertEqual(c5.pretty_printed_signature(verbose=False), c5_summary)

  def testPrettyPrintedExplicitSignatureWithKeywordArg(self):  # b/159639913

    @polymorphic_function.function(
        input_signature=[tensor_spec.TensorSpec(None)])
    def fn(a, b=1):
      return a + b

    concrete_fn = fn.get_concrete_function()
    self.assertEqual(concrete_fn.pretty_printed_signature(False), 'fn(a)')
    self.assertEqual(
        concrete_fn.pretty_printed_signature(True), 'fn(a)\n'
        '  Args:\n'
        '    a: float32 Tensor, shape=<unknown>\n'
        '  Returns:\n'
        '    float32 Tensor, shape=<unknown>')

  def testPrettyPrintedSignatureLoadedNamedTuple(self):
    Point = collections.namedtuple('Point', ['x', 'y'])

    @polymorphic_function.function
    def fn(b, a):  # pylint: disable=unused-argument
      return 1.

    b = Point(
        x=constant_op.constant(1., dtype=dtypes.float32),
        y=constant_op.constant(1., dtype=dtypes.float32))
    a = Point(
        x=constant_op.constant(1, dtype=dtypes.int32),
        y=constant_op.constant(1, dtype=dtypes.int32))

    mod = module.Module()
    f = fn.get_concrete_function(b, a)
    save(mod, '/tmp/f', signatures=f)
    loaded = load('/tmp/f')

    printed = loaded.signatures['serving_default'].pretty_printed_signature()
    self.assertIn('a: int32 Tensor, shape=()', printed)
    self.assertIn('a_1: int32 Tensor, shape=()', printed)
    self.assertIn('b: float32 Tensor, shape=()', printed)
    self.assertIn('b_1: float32 Tensor, shape=()', printed)

  @test_util.run_in_graph_and_eager_modes
  def testIndexedSlicesAsGradientsForConcreteFunctions(self):

    @polymorphic_function.function
    def summing_rnn(inputs):
      return math_ops.reduce_sum(inputs, axis=1)

    @polymorphic_function.function
    def gradients(inputs):
      with backprop.GradientTape() as tape:
        tape.watch(inputs)
        hidden = summing_rnn(inputs)
        hidden = array_ops.gather(hidden, constant_op.constant([0]))
        loss = math_ops.reduce_mean(hidden)
      return tape.gradient(loss, inputs)

    gradients(constant_op.constant([[[1.0], [2.0]]]))  # No error is raised

  def testFollowTypeHintsTraceBasic(self):
    trace_count = [0]

    def func(x: ops.Tensor):
      trace_count[0] += 1
      return x

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)
    disabled = polymorphic_function.function(
        func, experimental_follow_type_hints=False)

    enabled(1)  # Initial call gets traced
    enabled(2)
    enabled(3)
    self.assertEqual(trace_count[0], 1)

    trace_count = [0]
    disabled(1)
    disabled(2)  # Retrace
    disabled(3)  # Retrace
    self.assertEqual(trace_count[0], 3)

  def testFollowTypeHintsTraceWithArgs(self):
    trace_count = [0]

    def func(*args: ops.Tensor):
      trace_count[0] += 1
      return args

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)
    disabled = polymorphic_function.function(
        func, experimental_follow_type_hints=False)

    args = (
        'abc',
        'def',
    ) * 20
    args2 = (
        'def',
        'abc',
    ) * 20

    enabled(args)
    enabled(args2)
    self.assertEqual(trace_count[0], 1)

    trace_count = [0]
    disabled(args)
    disabled(args2)  # Retrace
    self.assertEqual(trace_count[0], 2)

  def testFollowTypeHintsTraceWithKwargs(self):
    trace_count = [0]

    def func(t: ops.Tensor, **kwargs: ops.Tensor):
      del kwargs
      trace_count[0] += 1
      return t

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)
    disabled = polymorphic_function.function(
        func, experimental_follow_type_hints=False)

    enabled(1, x=1, y=1.0, z='one')
    enabled(2, x=2, y=2.0, z='two')
    self.assertEqual(trace_count[0], 1)

    trace_count = [0]
    disabled(1, x=1, y=1.0, z='one')
    disabled(2, x=2, y=2.0, z='two')  # Retrace
    self.assertEqual(trace_count[0], 2)

  def testFollowTypeHintsTraceWithMultipleInputTypes(self):
    trace_count = [0]

    def func(t: ops.Tensor, *args: ops.Tensor, **kwargs: ops.Tensor):
      del args, kwargs
      trace_count[0] += 1
      return t

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)
    disabled = polymorphic_function.function(
        func, experimental_follow_type_hints=False)

    enabled(1, constant_op.constant(1), 'str', x=4.0)
    enabled(2, constant_op.constant(2), 'str2', x=5.0)
    self.assertEqual(trace_count[0], 1)

    trace_count = [0]
    disabled(1, constant_op.constant(1), 'str', x=4.0)
    disabled(2, constant_op.constant(2), 'str2', x=5.0)  # Retrace
    self.assertEqual(trace_count[0], 2)

  def testFollowTypeHintsTraceWithOnlyArgNamed(self):
    trace_count = [0]

    def func(t: ops.Tensor, i: int = 1, **kwargs):  # pylint: disable=bad-whitespace
      del i, kwargs
      trace_count[0] += 1
      return t

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(1, 3, x=4.0, y='str')
    enabled(2, 4, x=4.0, y='str')  # Retrace
    self.assertEqual(trace_count[0], 2)

  def testFollowTypeHintsTraceWithNotAllNamed(self):
    trace_count = [0]

    def func(x, y: ops.Tensor, z: int):
      del y, z
      trace_count[0] += 1
      return x

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(1, 2, 3)
    enabled(1, 20, 3)  # No retrace - change in ops.Tensor typed arg
    enabled(2, 2, 3)  # Retrace - change in untyped arg
    enabled(2, 2, 4)  # Retrace - change in typed arg
    self.assertEqual(trace_count[0], 3)

  def testFollowTypeHintsTraceWithOnlyArgsNamed(self):
    trace_count = [0]

    def func(x, y, *args: ops.Tensor):
      del y, args
      trace_count[0] += 1
      return x

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(1, 20, 3, 4, 5, 6)
    enabled(1, 20, 3, 4, 5, 60)  # No retrace - change in *args
    enabled(1, 30, 7, 8, 9, 10)  # Retrace - change in args
    self.assertEqual(trace_count[0], 2)

  def testFollowTypeHintsTraceWithOnlyKwargsNamed(self):
    trace_count = [0]

    def func(x, y, *args, **kwargs: ops.Tensor):
      del y, args, kwargs
      trace_count[0] += 1
      return x

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(1, 2, 3, 4, 5, 6, a=1.0, b=2.0, c=3.0)
    enabled(
        1, 2, 3, 4, 5, 6, a=1.5, b=2.5,
        c=3.5)  # No retrace - change in **kwargs
    enabled(100, 2, 3, 4, 5, 6, a=1.0, b=2.0, c=3.0)  # Retrace - change in args
    enabled(
        1, 2, 3, 4, 5, 100, a=1.0, b=2.0, c=3.0)  # Retrace - change in *args
    self.assertEqual(trace_count[0], 3)

  def testFollowTypeHintsTraceWithArgsEquals(self):
    trace_count = [0]

    def func(
        x: ops.Tensor = 0,  # pylint:disable=bad-whitespace
        y: int = 1,  # pylint:disable=bad-whitespace
        **kwargs: ops.Tensor):
      del y, kwargs
      trace_count[0] += 1
      return x

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(x=1, y=2, z=3)
    enabled(x=1, y=3, z=3)  # Retrace - change in args
    enabled(x=2, y=2, z=4)  # No retrace - change in args and **kwargs
    enabled(x=2, y=2, z=4, u=5)  # Retrace - change in **kwargs
    self.assertEqual(trace_count[0], 3)

  def testFollowTypeHintsWithTensorSpec(self):

    def func(x: ops.Tensor, y):
      return x + y

    v = polymorphic_function.function(experimental_follow_type_hints=True)(func)
    v = v.get_concrete_function(
        tensor_spec.TensorSpec(shape=None, dtype=dtypes.float32), 3)
    x = v(constant_op.constant(1.), 3)
    self.assertEqual(x.numpy(), 4.)

  def testFollowTypeHintsTraceWithKwArgsAndNoVarKws(self):
    trace_count = [0]

    def func(a: int, b: ops.Tensor, x: ops.Tensor = 0, y: int = 1):
      del a, b, y
      trace_count[0] += 1
      return x

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(0, 0, x=1, y=2)
    enabled(
        0,
        0,
        x=2,
        y=2,
    )  # No retrace, since only tensor changed
    self.assertEqual(trace_count[0], 1)

    # Pass args as keyword args.
    enabled(
        a=0,
        b=0,
        x=2,
        y=2,
    )  # No retrace, args are the same
    self.assertEqual(trace_count[0], 1)

    enabled(
        a=1,
        b=0,
        x=2,
        y=2,
    )  # Retrace, since non-tensor arg changed
    self.assertEqual(trace_count[0], 2)

    enabled(a=1, b=2, x=2, y=2)  # No retrace, since only tensor changed
    self.assertEqual(trace_count[0], 2)

    trace_count[0] = 0
    disabled = polymorphic_function.function(
        func, experimental_follow_type_hints=False)
    disabled(0, 0, x=1, y=2)
    disabled(
        0,
        0,
        x=2,
        y=2,
    )  # Retrace
    self.assertEqual(trace_count[0], 2)

  def testFollowTypeHintsTraceWithArgsEqualsTypedKwargs(self):
    trace_count = [0]

    def func(x, y, **kwargs: ops.Tensor):
      del y, kwargs
      trace_count[0] += 1
      return x

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(x=1, y=2, z=3)
    enabled(x=1, y=3, z=3)  # Retrace
    enabled(x=1, y=2, z=4)  # No retrace
    enabled(x=2, y=2, z=4)  # Retrace
    enabled(x=2, y=2, z=4, u=5)  # Retrace
    self.assertEqual(trace_count[0], 4)

  def testFollowTypeHintsTraceWithArgsEqualsTypedArgs(self):
    trace_count = [0]

    def func(x: ops.Tensor, y: int, **kwargs):
      del y, kwargs
      trace_count[0] += 1
      return x

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(x=1, y=2, z=3)
    enabled(x=1, y=3, z=3)  # Retrace
    enabled(x=1, y=2, z=4)  # Retrace
    enabled(x=2, y=2, z=3)  # No retrace
    enabled(x=2, y=2, z=4, u=5)  # Retrace
    self.assertEqual(trace_count[0], 4)

  def testFollowTypeHintsTraceWithKwOnlyArgsBasic(self):
    trace_count = [0]

    def func(*, a: ops.Tensor = None, b=1):  # pylint: disable=bad-whitespace
      del b
      trace_count[0] += 1
      return a

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(a=1, b=2)
    enabled(a=2, b=2)  # No retrace
    enabled(a=1, b=1)  # Retrace
    self.assertEqual(trace_count[0], 2)

  def testFollowTypeHintsTraceWithArgsKwOnlyArgsKwargsAndTypedArg(self):
    trace_count = [0]

    def func(arg: ops.Tensor, *args, kwonly, **kwargs):
      del args, kwonly, kwargs
      trace_count[0] += 1
      return arg

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(1, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)
    enabled(100, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)  # No retrace
    enabled(1000, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)  # No retrace
    enabled(1, 20, 30, 40, kwonly=5, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 2, 3, 4, kwonly=50, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 2, 3, 4, kwonly=5, kwarg1=60, kwarg2=70)  # Retrace
    self.assertEqual(trace_count[0], 4)

  def testFollowTypeHintsTraceWithArgsKwOnlyArgsKwargsAndTypedArgs(self):
    trace_count = [0]

    def func(arg, *args: ops.Tensor, kwonly, **kwargs):
      del args, kwonly, kwargs
      trace_count[0] += 1
      return arg

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(1, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)
    enabled(100, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 20, 30, 40, kwonly=5, kwarg1=6, kwarg2=7)  # No retrace
    enabled(1, 200, 300, 400, kwonly=5, kwarg1=6, kwarg2=7)  # No retrace
    enabled(1, 2, 3, 4, kwonly=50, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 2, 3, 4, kwonly=5, kwarg1=60, kwarg2=70)  # Retrace
    self.assertEqual(trace_count[0], 4)

  def testFollowTypeHintsTraceWithArgsKwOnlyArgsKwargsAndTypedKwOnlyArg(self):
    trace_count = [0]

    def func(arg, *args, kwonly: ops.Tensor, **kwargs):
      del args, kwonly, kwargs
      trace_count[0] += 1
      return arg

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(1, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)
    enabled(100, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 20, 30, 40, kwonly=5, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 2, 3, 4, kwonly=50, kwarg1=6, kwarg2=7)  # No retrace
    enabled(1, 2, 3, 4, kwonly=500, kwarg1=6, kwarg2=7)  # No retrace
    enabled(1, 2, 3, 4, kwonly=5, kwarg1=60, kwarg2=70)  # Retrace
    self.assertEqual(trace_count[0], 4)

  def testFollowTypeHintsTraceWithArgsKwOnlyArgsKwargsAndTypedKwargs(self):
    trace_count = [0]

    def func(arg, *args, kwonly, **kwargs: ops.Tensor):
      del args, kwonly, kwargs
      trace_count[0] += 1
      return arg

    enabled = polymorphic_function.function(
        func, experimental_follow_type_hints=True)

    enabled(1, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)
    enabled(100, 2, 3, 4, kwonly=5, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 20, 30, 40, kwonly=5, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 2, 3, 4, kwonly=50, kwarg1=6, kwarg2=7)  # Retrace
    enabled(1, 2, 3, 4, kwonly=5, kwarg1=60, kwarg2=70)  # No retrace
    enabled(1, 2, 3, 4, kwonly=5, kwarg1=600, kwarg2=700)  # No retrace
    self.assertEqual(trace_count[0], 4)

  def testWithExtraWrapper(self):

    class Foo(module.Module):

      def __init__(self):
        super().__init__()
        self.var = None

      @polymorphic_function.function
      @dummy_tf_decorator
      def add(self, x, y, z=1):
        if self.var is None:
          return x + y + z

    foo = Foo()
    self.assertEqual(foo.add(2, 3).numpy(), 6)

  @parameterized.parameters([
      (polymorphic_function.function, dummy_tf_decorator),
      (dummy_tf_decorator, polymorphic_function.function),
      (polymorphic_function.function, polymorphic_function.function)
  ])
  def testWithExtraWrapperRedundantArgs(self, decorator1, decorator2):

    class Foo(module.Module):

      def __init__(self):
        super().__init__()
        self.var = None

      @decorator1
      @decorator2
      def add1(self, x, y):
        if self.var is None:
          return x + y

    foo = Foo()
    with self.assertRaisesRegex(TypeError, 'got two values'):
      foo.add1(2, x=3)  # pylint: disable=redundant-keyword-arg,no-value-for-parameter

  def testWithExtraWrapperMissingArgs(self):

    class Foo(module.Module):

      def __init__(self):
        super().__init__()
        self.var = None

      @polymorphic_function.function
      @dummy_tf_decorator
      def add1(self, x, y):
        if self.var is None:
          return x + y

      @polymorphic_function.function
      @dummy_tf_decorator
      def add2(self, x, y):
        if self.var is None:
          return x + y

      @polymorphic_function.function
      @polymorphic_function.function
      def add3(self, x, y):
        if self.var is None:
          return x + y

    foo = Foo()
    with self.assertRaisesRegex(
        TypeError, 'missing 1 required positional argument: \'y\''):
      foo.add1(2)  # pylint: disable=no-value-for-parameter

    with self.assertRaisesRegex(TypeError, 'missing 1 required argument: x'):
      foo.add1(y=2)  # pylint: disable=no-value-for-parameter

    with self.assertRaisesRegex(
        TypeError, 'missing 1 required positional argument: \'y\''):
      foo.add2(2)  # pylint: disable=no-value-for-parameter

    with self.assertRaisesRegex(TypeError, 'missing 1 required argument: x'):
      foo.add2(y=2)  # pylint: disable=no-value-for-parameter

    with self.assertRaisesRegex(
        TypeError, 'missing 1 required positional argument: \'y\''):
      foo.add3(2)  # pylint: disable=no-value-for-parameter

    with self.assertRaisesRegex(TypeError, 'missing 1 required argument: x'):
      foo.add3(y=2)  # pylint: disable=no-value-for-parameter

  def testMissingArgsTfFunctionedMethod(self):

    class A:

      def func(self, position_arg1, position_arg2):
        return position_arg1, position_arg2

      @polymorphic_function.function
      def decorated_method(self, position_arg1, position_arg2):
        return position_arg1, position_arg2

    a_instance = A()
    tf_method_pos = polymorphic_function.function(a_instance.func)
    with self.assertRaisesRegex(
        TypeError, '.* missing 1 required argument: position_arg1'):
      tf_method_pos(position_arg2='foo')

    # tf.function-decorated instance methods need to be tested because of
    # the __get__ method implementation.
    tf_func_decorated_method = polymorphic_function.function(
        a_instance.decorated_method)
    tf_func_decorated_method(position_arg1='foo', position_arg2='bar')
    with self.assertRaisesRegex(
        TypeError, '.* missing 1 required argument: position_arg1'):
      tf_func_decorated_method(position_arg2='bar')

  def testMissingArgsTfFunctionedObject(self):

    class A:

      def __call__(self, position_arg1, position_arg2):
        return position_arg1, position_arg2

    a_instance = A()

    # A tf.function-decorated callable object needs to be tested because of
    # the special inspect results.
    tf_func_obj = polymorphic_function.function(a_instance)
    tf_func_obj(position_arg1=1, position_arg2=2)
    with self.assertRaisesRegex(
        TypeError, '.* missing 1 required argument: position_arg1'):
      tf_func_obj(position_arg2='bar')

  def testMissingArgsTfFunctionedFunctions(self):

    def func_pos(position_arg1, position_arg2):
      return position_arg1, position_arg2

    def func_with_default(position_arg, named_arg=None):
      return position_arg, named_arg

    def func_pos_3args(position_arg1, position_arg2, position_arg3):
      return position_arg1, position_arg2, position_arg3

    tf_func_pos = polymorphic_function.function(func_pos)
    with self.assertRaisesRegex(
        TypeError, '.* missing 1 required argument: position_arg1'):
      tf_func_pos(position_arg2='foo')

    tf_func_with_default = polymorphic_function.function(func_with_default)
    tf_func_with_default(position_arg='bar')
    with self.assertRaisesRegex(TypeError,
                                '.* missing 1 required argument: position_arg'):
      tf_func_with_default(named_arg='foo')

    tf_func_pos_3args = polymorphic_function.function(func_pos_3args)
    with self.assertRaisesRegex(
        TypeError,
        '.* missing required arguments: position_arg1, position_arg3'):
      tf_func_pos_3args(position_arg2='foo')

  def testShapeInferencePropagateConstNestedStack(self):

    @polymorphic_function.function(input_signature=[
        tensor_spec.TensorSpec((None, None), dtype=dtypes.int32),
        tensor_spec.TensorSpec((), dtype=dtypes.int32),
    ])
    def f(x, s):
      old_shape = array_ops.shape(x)
      new_shape = array_ops.stack([old_shape[0], s], axis=0)
      y = array_ops.ones(shape=new_shape, dtype=dtypes.int32)
      return y

    @polymorphic_function.function(input_signature=[
        tensor_spec.TensorSpec(shape=(3, 6), dtype=dtypes.int32)
    ])
    def g(x):
      y = f(x, s=5)
      assert y.shape.as_list() == [3, 5], y.shape.as_list()
      return y

    self.assertAllEqual(
        g(array_ops.zeros([3, 6], dtype=dtypes.int32)), array_ops.ones([3, 5]))

  def testShapeInferencePropagateConstNestedUnstackStack(self):

    @polymorphic_function.function(input_signature=[
        tensor_spec.TensorSpec((None, None), dtype=dtypes.int32),
        tensor_spec.TensorSpec((), dtype=dtypes.int32),
    ])
    def f(x, s):
      s0, _ = array_ops.unstack(array_ops.shape(x), axis=0)
      new_shape = array_ops.stack([s0, s], axis=0)
      y = array_ops.ones(shape=new_shape, dtype=dtypes.int32)
      return y

    @polymorphic_function.function(input_signature=[
        tensor_spec.TensorSpec(shape=(3, 6), dtype=dtypes.int32)
    ])
    def g(x):
      y = f(x, s=5)
      assert y.shape.as_list() == [3, 5], y.shape.as_list()
      return y

    self.assertAllEqual(
        g(array_ops.zeros([3, 6], dtype=dtypes.int32)), array_ops.ones([3, 5]))

  def testShapeInferencePropagateConstNestedConcat(self):

    @polymorphic_function.function(input_signature=[
        tensor_spec.TensorSpec((), dtype=dtypes.int32),
        tensor_spec.TensorSpec((), dtype=dtypes.int32),
        tensor_spec.TensorSpec((), dtype=dtypes.int32),
    ])
    def f(d1, d2, d3):
      new_shape = array_ops.concat([[d1], [d2], [d3]], axis=-1)
      y = array_ops.ones(shape=new_shape, dtype=dtypes.int32)
      return y

    @polymorphic_function.function()
    def g():
      y = f(1, 2, 3)
      assert y.shape.as_list() == [1, 2, 3], y.shape.as_list()
      return y

    self.assertAllEqual(g(), array_ops.ones([1, 2, 3]))

  def testShapeInferencePropagateConstDoubleNested(self):

    @polymorphic_function.function(input_signature=[
        tensor_spec.TensorSpec((), dtype=dtypes.int32),
        tensor_spec.TensorSpec((), dtype=dtypes.int32),
        tensor_spec.TensorSpec((), dtype=dtypes.int32),
    ])
    def f(d1, d2, d3):
      new_shape = array_ops.concat([[d1], [d2], [d3]], axis=-1)
      y = array_ops.ones(shape=new_shape, dtype=dtypes.int32)
      return y

    @polymorphic_function.function()
    def g():
      y = polymorphic_function.function(f)(1, 2, 3)
      assert y.shape.as_list() == [1, 2, 3], y.shape.as_list()
      return y

    self.assertAllEqual(g(), array_ops.ones([1, 2, 3]))

  @test_util.run_v2_only
  def testControlDependencyAfterInline(self):
    v = variables.Variable(0.)

    @polymorphic_function.function
    def assign():
      return v.assign(1.)

    @polymorphic_function.function
    def assign_add():
      return v.assign_add(1.)

    @polymorphic_function.function
    def f():
      check_ops.assert_equal_v2(assign(), 1.)
      check_ops.assert_equal_v2(assign_add(), 2.)

    # We don't have a way to inspect the inlined graph in Python, so we run it
    # multiple times to have more confidence the dependency is correct.
    for _ in range(30):
      f()

  @test_util.run_v2_only
  def testReadInFuncWriteOutside(self):
    # Run many times since we are testing for a potential race condition.
    for _ in range(30):
      # pylint: disable=cell-var-from-loop
      v = variables.Variable(1.)

      @polymorphic_function.function
      def add_one():
        return v + 1.

      @polymorphic_function.function
      def get_v_plus_one():
        v_plus_one = add_one()
        v.assign_add(2.0)
        return v_plus_one

      self.assertAllEqual(get_v_plus_one(), 2.0)

  def testOpExpandErrorMessage(self):

    @polymorphic_function.function
    def test_fn():
      if array_ops.constant(False):
        return array_ops.constant(1)
      else:
        return script_ops.eager_py_func(
            func=lambda: array_ops.constant([2.]), inp=(), Tout=dtypes.int32)

    error_pattern = re.compile(r'Graph execution error.*func=lambda', re.DOTALL)
    with self.assertRaisesRegex(errors.InvalidArgumentError, error_pattern):
      test_fn()


class MultiDeviceTest(test.TestCase, parameterized.TestCase):

  def testNestedCallWatchedVariables(self):

    v = variables.Variable(4.)

    @polymorphic_function.function
    def f():
      return v**2.

    with backprop.GradientTape() as tape:
      f()

    self.assertEqual((v,), tape.watched_variables())

    @polymorphic_function.function
    def g():
      return f()

    with backprop.GradientTape() as tape:
      g()

    self.assertEqual((v,), tape.watched_variables())

    # f() can rely on the variable being read during its trace. g() checks that
    # variables from a function which knows about them are recorded on the
    # tape. h() tests that functions forward knowledge of variables to callers.

    @polymorphic_function.function
    def h():
      return g()

    with backprop.GradientTape() as tape:
      h()

    self.assertEqual((v,), tape.watched_variables())

  def testReplaceCaptureWithDeferred(self):

    x = constant_op.constant(1.0)
    y = constant_op.constant(2.0)
    z = constant_op.constant(3.0)

    @polymorphic_function.function
    def fn():
      a = x + y
      b = a + z
      return b

    concrete_fn = fn.get_concrete_function()
    self.assertAllEqual(concrete_fn(), 6.0)

    value = constant_op.constant(4.0)

    def closure():
      return value

    concrete_fn.replace_capture_with_deferred_capture(
        concrete_fn.captured_inputs[1],
        closure,
        spec=tensor_spec.TensorSpec(shape=(), dtype=dtypes.float32),
        placeholder=concrete_fn.inputs[1])

    self.assertAllEqual(concrete_fn(), 8.0)

    value = constant_op.constant(5.0)
    self.assertAllEqual(concrete_fn(), 9.0)

  def testRaiseReplaceCaptureWithDeferredTypeSpecMismatch(self):
    bool_captured_tensor = constant_op.constant(True)
    float_captured_tensor = constant_op.constant([3.], dtype=dtypes.float32)
    value = constant_op.constant([2.], dtype=dtypes.float32)

    @polymorphic_function.function
    def fn():
      deferred_tensor = ops.get_default_graph().capture_call_time_value(
          lambda: value,
          tensor_spec.TensorSpec(shape=(1,), dtype=dtypes.float32))
      if bool_captured_tensor:
        return deferred_tensor
      else:
        return deferred_tensor + float_captured_tensor

    concrete_fn = fn.get_concrete_function()
    self.assertAllEqual(concrete_fn(), [2.])

    new_bool_captured_tensor = constant_op.constant(False)

    def bool_closure():
      return new_bool_captured_tensor

    # Test raise if replacing a bool capture with a closure of output type
    # float32
    new_float_captured_tensor = constant_op.constant([3.], dtype=dtypes.float32)

    def float_closure():
      return new_float_captured_tensor

    with self.assertRaisesRegex(ValueError,
                                'Attempting to substitute closure with spec*'):
      concrete_fn.replace_capture_with_deferred_capture(
          bool_captured_tensor,
          float_closure,
          spec=tensor_spec.TensorSpec(shape=(1,), dtype=dtypes.float32))

    # Test replace without a placeholder
    concrete_fn.replace_capture_with_deferred_capture(
        bool_captured_tensor,
        bool_closure,
        spec=tensor_spec.TensorSpec(shape=(), dtype=dtypes.bool))

    self.assertAllEqual(concrete_fn(), [5.])

  def testConcreteFunctionSetExternalCapture(self):
    captured_tensor = constant_op.constant([1.])
    value = constant_op.constant([2.])

    @polymorphic_function.function
    def fn():
      deferred_tensor = ops.get_default_graph().capture_call_time_value(
          lambda: value,
          tensor_spec.TensorSpec(shape=(1,), dtype=dtypes.float32))
      return deferred_tensor + captured_tensor

    cf = fn.get_concrete_function()
    self.assertLen(cf._captured_inputs, 2)
    self.assertEqual(list(map(callable, cf._captured_inputs)), [False, True])
    self.assertAllEqual(cf(), [3.])

    # Reset capture to a deferred one, reset deferred capture to a capture.
    cf.set_external_captures([cf._captured_inputs[1], cf._captured_inputs[0]])

    value = constant_op.constant([3.])
    self.assertAllEqual(cf(), [4.])

  def testGraphReplaceCaptureAndSetExternalCapture(self):
    bool_captured_tensor = constant_op.constant(True)
    float_captured_tensor = constant_op.constant([3.], dtype=dtypes.float32)
    value = constant_op.constant([2.], dtype=dtypes.float32)

    @polymorphic_function.function
    def fn():
      deferred_tensor = ops.get_default_graph().capture_call_time_value(
          lambda: value,
          tensor_spec.TensorSpec(shape=(1,), dtype=dtypes.float32))
      if bool_captured_tensor:
        return deferred_tensor
      else:
        return deferred_tensor + float_captured_tensor

    concrete_fn = fn.get_concrete_function()
    self.assertAllEqual(concrete_fn(), [2.])

    new_bool_captured_tensor = constant_op.constant(False)

    def closure():
      return new_bool_captured_tensor

    concrete_fn.graph.replace_capture_with_deferred_capture(
        concrete_fn.captured_inputs[0],
        closure,
        spec=tensor_spec.TensorSpec(shape=(), dtype=dtypes.bool),
        placeholder=concrete_fn.inputs[1])

    concrete_fn.set_external_captures([
        closure, concrete_fn._captured_inputs[1],
        concrete_fn._captured_inputs[2]
    ])
    self.assertAllEqual(concrete_fn(), [5.])

  def testDeferredCapture(self):
    value = 1.0

    @polymorphic_function.function
    def lazy_capture(x):
      y = ops.get_default_graph().capture_call_time_value(
          lambda: value, tensor_spec.TensorSpec(None))
      return x + y

    self.assertAllEqual(lazy_capture(2.0), 3.0)
    # After changing the value of `value` the function call should return a
    # different result.
    value = 2.0
    self.assertAllEqual(lazy_capture(2.0), 4.0)

  def testNestedDeferredCapture(self):
    value = 1.0

    @polymorphic_function.function
    def inner(x):
      y = ops.get_default_graph().capture_call_time_value(
          lambda: value, tensor_spec.TensorSpec(None))
      return x + y

    @polymorphic_function.function
    def outer(x):
      return inner(x)

    self.assertAllEqual(outer(2.0), 3.0)
    # After changing the value of `value` the function call should return a
    # different result.
    value = 2.0
    self.assertAllEqual(outer(2.0), 4.0)

  def testNestedDeferredCaptureInTFWhileLoop(self):

    value = 1.

    @polymorphic_function.function
    def inner(x):
      y = ops.get_default_graph().capture_call_time_value(
          lambda: value, tensor_spec.TensorSpec(None))
      return x + y

    @polymorphic_function.function
    def outer():
      dummy = constant_op.constant(True)
      sums = constant_op.constant(0.)
      while dummy:
        directives.set_loop_options(
            shape_invariants=[(sums, tensor_shape.TensorShape(None))])
        sums += inner(2.)
        dummy = constant_op.constant(False)
      return sums

    self.assertAllEqual(outer(), 3.)

    value = constant_op.constant(2.)
    self.assertAllEqual(outer(), 4.)

    value = constant_op.constant(3.)
    self.assertAllEqual(outer(), 5.)

  def testDeferredCaptureWithKey(self):
    value0 = 1.0
    value1 = 2.0

    @polymorphic_function.function
    def lazy_capture(x):
      w = ops.get_default_graph().capture_call_time_value(
          lambda: value0, tensor_spec.TensorSpec(None), key=0)
      y = ops.get_default_graph().capture_call_time_value(
          lambda: value1, tensor_spec.TensorSpec(None), key=1)

      def bad_closure():
        raise ValueError('Should not run')

      z = ops.get_default_graph().capture_call_time_value(
          bad_closure, tensor_spec.TensorSpec(None), key=1)
      return x + y + w + z

    self.assertAllEqual(lazy_capture(2.0), 7.0)
    value0 = 2.0
    value1 = 3.0
    self.assertAllEqual(lazy_capture(2.0), 10.0)

  def testDeferredCaptureTypeError(self):
    value = constant_op.constant(1.0)

    @polymorphic_function.function
    def lazy_capture(x):
      y = ops.get_default_graph().capture_call_time_value(
          lambda: value, tensor_spec.TensorSpec(()))
      return x + y

    self.assertAllEqual(lazy_capture(2.0), 3.0)

    # dtype mismatch
    value = constant_op.constant(1)
    with self.assertRaisesRegex(ValueError, 'Value .* to a tensor with dtype'):
      lazy_capture(2.0)

    # shape mismatch
    value = constant_op.constant([1.0])
    with self.assertRaisesRegex(ValueError, 'Value .* shape'):
      lazy_capture(2.0)

  def testDeferredCaptureReturnNestWithCompositeTensor(self):
    i_s = indexed_slices.IndexedSlices(
        constant_op.constant([1, 2]),
        constant_op.constant([0, 1], dtype=dtypes.int64),
        constant_op.constant([2]))
    r_t = ragged_factory_ops.constant([[[1, 2], [3]], [[4, 5, 6]]])
    s_t = sparse_tensor.SparseTensor(
        values=[1, 2, 3], indices=[[0], [8], [10]], dense_shape=[20])

    @polymorphic_function.function
    def lazy_capture():
      y = ops.get_default_graph().capture_call_time_value(
          lambda: {'i': i_s, 't': (r_t, s_t)},
          {'i': indexed_slices.IndexedSlicesSpec(
              dtype=dtypes.int32, dense_shape_dtype=dtypes.int32),
           't': (ragged_tensor.RaggedTensorSpec([2, None, None], dtypes.int32),
                 sparse_tensor.SparseTensorSpec([None], dtypes.int32))})
      return y['i'], y['t']

    i, (r, s) = lazy_capture()
    self.assertAllEqual(i_s.values, i.values)
    self.assertAllEqual(i_s.indices, i.indices)
    self.assertAllEqual(i_s.dense_shape, i.dense_shape)
    self.assertAllEqual(r_t, r)
    self.assertAllEqual(s_t.indices, s.indices)
    self.assertAllEqual(s_t.values, s.values)
    self.assertAllEqual(s_t.dense_shape, s.dense_shape)

  def testDeferredCaptureCompositeTensorSpecTypeMismatch(self):
    value = indexed_slices.IndexedSlices(
        constant_op.constant([1, 2]),
        constant_op.constant([0, 1], dtype=dtypes.int64))

    @polymorphic_function.function
    def lazy_capture():
      return ops.get_default_graph().capture_call_time_value(
          lambda: value, indexed_slices.IndexedSlicesSpec(dtype=dtypes.int32))

    # Type matches spec.
    lazy_capture()

    # Extra dense shape component.
    value = indexed_slices.IndexedSlices(
        constant_op.constant([1, 2]),
        constant_op.constant([0, 1], dtype=dtypes.int64),
        constant_op.constant([2]))
    with self.assertRaises(ValueError):
      lazy_capture()

    # Index dtype mismatch int32 vs. int64.
    value = indexed_slices.IndexedSlices(
        constant_op.constant([1, 2]), constant_op.constant([0, 1]))
    with self.assertRaises(ValueError):
      lazy_capture()

  def testMaybeCreateCapturePlaceholderWithValidCapture(self):

    @polymorphic_function.function
    def f():
      func = lambda: x
      return ops.get_default_graph()._maybe_create_capture_placeholder(func)

    x = {
        'tensor': constant_op.constant(0),
        'list': [constant_op.constant(1), 2],
        'dict': {
            'float': constant_op.constant(0.5)
        }
    }

    out = f()
    # tf.function output should have same structure/values with the side input
    self.assertEqual(x['tensor'].numpy(), out['tensor'].numpy())
    self.assertEqual(x['list'][0].numpy(), out['list'][0].numpy())
    self.assertEqual(x['list'][1], out['list'][1].numpy())
    self.assertEqual(x['dict']['float'].numpy(), out['dict']['float'].numpy())

  def testMaybeCreateCapturePlaceholderWithInvalidCapture(self):

    @polymorphic_function.function
    def f():
      func = lambda: x
      return ops.get_default_graph()._maybe_create_capture_placeholder(func)

    # Set is not supported
    x = set([1, 2])
    with self.assertRaises(NotImplementedError):
      f()

  # TODO(panzf): remove this test after exposing manual API, as the integration
  # testcase can be turned on at that time.
  def test_inner_nested_tf_function_raise_error(self):

    @polymorphic_function.function
    def tf_f():

      @polymorphic_function.function
      def tf_g():
        cx = ops.get_default_graph()._experimental_capture_side_input_by_ref(  # pylint: disable=protected-access
            'lambda: x', lambda: x)
        return cx

      return tf_g()

    x = constant_op.constant(0)  # pylint: disable=unused-variable
    with self.assertRaisesRegex(NotImplementedError,
                                'Manual side input usage for inner nested'):
      tf_f()

  @parameterized.parameters(
      (1, int, 2, int, 2),
      (1, constant_op.constant, 2, constant_op.constant, 1))
  def testRetraceLogicWithSideInputs(self, val_before, type_before, val_after,
                                     type_after, expected_len):

    @polymorphic_function.function
    def f():
      func = lambda: x
      return ops.get_default_graph()._experimental_capture_side_input_by_ref(  # pylint: disable=protected-access
          'lambda: x', func)

    x = type_before(val_before)
    _ = f()
    x = type_after(val_after)
    _ = f()
    self.assertLen(total_function_cache(f), expected_len)

  def testFunctoolsLruCache(self):
    self.skipTest(
        "b/194845243: inspect.getfullargspec doesn't unwrap Python decorators.")

    @polymorphic_function.function
    @functools.lru_cache(maxsize=2)
    def f(a):
      return 2 * a

    self.assertAllEqual(f(1), array_ops.constant(2))


if __name__ == '__main__':
  ops.enable_eager_execution()
  test.main()
import io
import os
import sys

import torch
import torch.nn as nn

# Make the helper files in test/ importable
pytorch_test_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(pytorch_test_dir)
from torch.testlib.jit_utils import JitTestCase, _inline_everything
from torch.testlib.common_utils import TemporaryFileName

class TestAsync(JitTestCase):
    def test_async_python(self):
        @torch.jit.script
        def foo(x):
            return torch.neg(x)

        x = torch.rand(3, 4)
        fut = torch.jit._fork(foo, x)
        y_hat = foo(x)
        y = torch.jit._wait(fut)
        # assert nothing; only to make sure the fake python path works

    def test_async_parsing(self):
        @torch.jit.script
        def foo(x):
            # type: (Tensor) -> List[Tensor]
            return [torch.neg(x), x.t()]

        @torch.jit.script
        def bar(x):
            futures = torch.jit.annotate(List[Future[List[Tensor]]], [])
            for _ in range(3):
                future = torch.jit.annotate(
                    Future[List[Tensor]],
                    torch.jit._fork(foo, x)
                )
                futures.append(future)

            output = torch.jit.annotate(List[List[Tensor]], [])
            for i in range(3):
                output.append(torch.jit._wait(futures[i]))
            return output

        x = torch.rand(3, 3)
        result = bar(x)
        self.assertEqual(len(result), 3)

    def test_async_script(self):
        @torch.jit.script
        def foo(x):
            return torch.neg(x), x

        x = torch.rand(3, 4)

        @torch.jit.script
        def wait_script(x):
            fut = torch.jit._fork(foo, x)
            y_hat = foo(x)
            y = torch.jit._wait(fut)
            return y, y_hat

        y, y_hat = wait_script(x)

        self.assertEqual(y, y_hat)

    def test_async_script_capture(self):
        class Mod(torch.jit.ScriptModule):
            __constants__ = ['const']

            def __init__(self):
                super(Mod, self).__init__()
                self.const = 42
                self.param = nn.Parameter(torch.randn(2, 2))

            @torch.jit.script_method
            def foo(self, x1, x2):
                return torch.neg(x1), self.param, self.const, torch.neg(x2), self.param

            @torch.jit.script_method
            def forward(self, x1, x2):
                fut = torch.jit._fork(self.foo, x1, x2)
                y_hat = self.foo(x1, x2)
                y = torch.jit._wait(fut)
                return y, y_hat

        x1 = torch.rand(3, 4)
        x2 = torch.rand(5, 6)

        m = Mod()

        with torch.jit.optimized_execution(False):
            y, y_hat = m.forward(x1, x2)

        self.assertEqual(y, y_hat)

    def test_async_script_nested(self):
        @torch.jit.script
        def foo(x):
            return torch.neg(x), x

        x = torch.rand(3, 4)

        @torch.jit.script
        def wait_script(x):
            fut = torch.jit._fork(foo, x)
            y_hat = foo(x)
            y = torch.jit._wait(fut)
            return y, y_hat

        @torch.jit.script
        def wait_script_nest(x):
            fut = torch.jit._fork(wait_script, x)
            return torch.jit._wait(fut)

        y, y_hat = wait_script_nest(x)

        self.assertEqual(y, y_hat)

    def test_async_script_no_script_mod(self):
        x = torch.rand(3, 4)

        with self.assertRaisesRegex(RuntimeError, 'cannot call a value'):
            @torch.jit.script
            def wait_script(x):
                fut = torch.jit._fork(x)
                return fut

    def test_async_script_multi_waits(self):
        @torch.jit.script
        def foo(x):
            return torch.neg(x).t() + x

        @torch.jit.script
        def wait_script(x):
            fut = torch.jit._fork(foo, x)

            # wait twice on the same future
            y1 = torch.jit._wait(fut)
            y2 = torch.jit._wait(fut)
            return y1, y2

        x = torch.rand(2, 2)
        y1, y2 = wait_script(x)
        self.assertEqual(y1, y2)

    def test_async_script_multi_forks(self):
        @torch.jit.script
        def foo1(x):
            return torch.neg(x).t() + x

        @torch.jit.script
        def foo2(x, y):
            return torch.neg(x).t() + x + torch.neg(y).t()

        @torch.jit.script
        def foo3(x, y, z):
            return torch.neg(z).t() + y.t() + x

        x1 = torch.rand(10, 10)
        x2 = torch.rand(10, 10)
        x3 = torch.rand(10, 10)

        @torch.jit.script
        def wait_script(x1, x2, x3):
            f1 = torch.jit._fork(foo1, x1)
            f2 = torch.jit._fork(foo2, x1, x2)
            f3 = torch.jit._fork(foo3, x1, x2, x3)
            f4 = torch.jit._fork(foo1, x2)
            f5 = torch.jit._fork(foo2, x2, x3)

            # ignore some forks
            y1 = torch.jit._wait(f1)
            y2 = torch.jit._wait(f2)
            y3 = torch.jit._wait(f3)

            return y1, y2, y3

        y1, y2, y3 = wait_script(x1, x2, x3)
        self.assertEqual(y1, foo1(x1))
        self.assertEqual(y2, foo2(x1, x2))
        self.assertEqual(y3, foo3(x1, x2, x3))

    @_inline_everything
    def test_async_script_trace(self):
        class Traced(nn.Module):
            def __init__(self):
                super(Traced, self).__init__()

            def forward(self, x):
                return (torch.neg(x), x)

        class Mod(torch.jit.ScriptModule):
            def __init__(self):
                super(Mod, self).__init__()
                x = torch.rand(3, 3)
                self.traced = torch.jit.trace(Traced(), (x), _force_outplace=True)

            @torch.jit.script_method
            def forward(self, x):
                # type: (Tensor) -> Tuple[List[Tensor], Tuple[Tensor, Tensor], Tensor]
                future1 = torch.jit._fork(self.traced, x)
                future2 = torch.jit._fork(torch.neg, x)

                tensor_tuple = torch.jit._wait(future1)
                tensor_single = torch.jit._wait(future2)

                tensor_list = []
                tensor_list.append(tensor_tuple[0])
                tensor_list.append(tensor_single)

                # return a nested structure of tensors
                return (tensor_list, tensor_tuple, tensor_tuple[1])

        class TupleCl(nn.Module):
            def __init__(self):
                super(TupleCl, self).__init__()
                self.module = Mod()

            def forward(self, x):
                z = torch.neg(x)
                y = self.module(x)
                list = [z, y[0][0], y[0][1], y[1][0], y[1][1], y[2]]
                return tuple(list)

        x = torch.rand(3, 3)
        module = torch.jit.trace(TupleCl(), (x), _force_outplace=True)

        # Make sure we have forks
        self.assertGraphContainsExactly(module.graph, kind='prim::fork', num_kind_nodes=2)
        # Make sure 1 ::neg is in the root graph and 2 ::negs are in the subgraphs
        self.assertGraphContainsExactly(module.graph, kind='aten::neg', num_kind_nodes=1)
        self.assertGraphContainsExactly(module.graph, kind='aten::neg', num_kind_nodes=3, consider_subgraphs=True)

        y = torch.neg(x)
        self.assertEqual(module(x), (y, y, y, y, x, x))

    def test_async_script_error(self):
        x = torch.rand(3, 4)

        @torch.jit.script
        def foo(x):
            # error here
            return x.t() + x

        @torch.jit.script
        def wait_script(x):
            fut = torch.jit._fork(foo, x)
            return torch.jit._wait(fut)

        @torch.jit.script
        def wait_script_nest(x):
            fut = torch.jit._fork(wait_script, x)
            return torch.jit._wait(fut)

        # no future
        error_msg = 'The size.*must match the size of tensor'
        with self.assertRaisesRegex(Exception, error_msg):
            foo(x)

        # one future
        with self.assertRaisesRegex(Exception, error_msg):
            wait_script(x)

        # two futures with a different error
        x = torch.rand(3, 4, 5)
        with self.assertRaisesRegex(Exception, 'expects a tensor with <= 2 dimensions'):
            wait_script_nest(x)

    def test_async_grad_guard_with_grad(self):
        @torch.jit.script
        def foo(x):
            y = x * 2
            return y.requires_grad

        @torch.jit.script
        def bar(x):
            fut = torch.jit._fork(foo, x)
            requires_grad_in_fork = torch.jit._wait(fut)
            z = x * 2
            return (requires_grad_in_fork, z.requires_grad)

        x = torch.randn(3, requires_grad=True)

        with torch.enable_grad():
            (inside_fork, after_wait) = bar(x)

        self.assertEqual(inside_fork, True)
        self.assertEqual(after_wait, True)

    def test_async_grad_guard_no_grad(self):
        @torch.jit.script
        def foo(x):
            y = x * 2
            return y.requires_grad

        @torch.jit.script
        def bar(x):
            fut = torch.jit._fork(foo, x)
            requires_grad_in_fork = torch.jit._wait(fut)
            z = x * 2
            return (requires_grad_in_fork, z.requires_grad)

        x = torch.randn(3, requires_grad=True)

        with torch.no_grad():
            (inside_fork, after_wait) = bar(x)

        self.assertEqual(inside_fork, False)
        self.assertEqual(after_wait, False)

    def test_trace_fork_wait(self):
        def fork_body(x):
            return x.neg(), x.neg() + 1

        def fn(x):
            fut = torch.jit._fork(fork_body, x)
            vals = torch.jit._wait(fut)
            return vals[0], vals[1], x - 1

        traced = torch.jit.trace(fn, (torch.rand(3, 4),))
        x = torch.rand(3, 4)
        self.assertEqual(fn(x), traced(x))

        self.assertGraphContainsExactly(traced.graph, kind='prim::fork', num_kind_nodes=1)
        self.assertGraphContainsExactly(traced.graph, kind='aten::wait', num_kind_nodes=1)
        self.assertGraphContainsExactly(traced.graph, kind='aten::neg', num_kind_nodes=2, consider_subgraphs=True)

    def test_trace_fork_wait_leaking(self):
        my_list = []

        def fork_body(x):
            my_list.append(x + 1)
            return x + 1

        def fn(x):
            fut = torch.jit._fork(fork_body, x)
            val = torch.jit._wait(fut)
            return my_list[0]

        with self.assertRaisesRegex(RuntimeError, 'did not have observable data dependence with trace inputs; '
                                                  'this probably indicates your program cannot be understood '
                                                  'by the tracer.'):
            traced = torch.jit.trace(fn, (torch.rand(3, 4),), check_trace=False)

    def test_trace_fork_wait_inline(self):
        def fork_body(x):
            return x + 1, x + 2

        def fn(x):
            fut = torch.jit._fork(fork_body, x)
            val = torch.jit._wait(fut)
            return val[1]

        traced = torch.jit.trace(fn, (torch.rand(3, 4),))
        torch._C._jit_pass_inline_fork_wait(traced.graph)
        torch._C._jit_pass_dce(traced.graph)
        self.assertGraphContainsExactly(traced.graph, kind='prim::fork', num_kind_nodes=0)
        self.assertGraphContainsExactly(traced.graph, kind='aten::wait', num_kind_nodes=0)
        self.assertGraphContainsExactly(traced.graph, kind='aten::add', num_kind_nodes=2)

    def test_trace_fork_wait_inline_onnx(self):
        def fork_body(x):
            return torch.neg(x), torch.neg(x)

        class MyMod(torch.nn.Module):
            def forward(self, x):
                fut = torch.jit._fork(fork_body, x)
                val = torch.jit._wait(fut)
                return val[1]

        # smoke test for ONNX export
        f = io.BytesIO()
        torch.onnx.export(MyMod(), (torch.rand(3, 4),), f)

    def test_save_load_with_extra_files(self):
        class MyMod(torch.jit.ScriptModule):
            @torch.jit.script_method
            def forward(self, a):
                return a

        expected_extra_files = torch._C.ExtraFilesMap()
        expected_extra_files['foo'] = 'bar'
        m = MyMod()

        # Save to file.
        with TemporaryFileName() as fname:
            m.save(fname, _extra_files=expected_extra_files)
            extra_files = torch._C.ExtraFilesMap()
            extra_files['foo'] = ''
            torch.jit.load(fname, _extra_files=extra_files)
            self.assertEqual('bar', extra_files['foo'])

            # Use torch.jit API
            torch.jit.save(m, fname, _extra_files=expected_extra_files)
            extra_files['foo'] = ''
            torch.jit.load(fname, _extra_files=extra_files)
            self.assertEqual('bar', extra_files['foo'])

        # Save to buffer.
        buffer = io.BytesIO(m.save_to_buffer(_extra_files=expected_extra_files))
        extra_files = torch._C.ExtraFilesMap()
        extra_files['foo'] = ''
        torch.jit.load(buffer, _extra_files=extra_files)
        self.assertEqual('bar', extra_files['foo'])

        # Use torch.jit API
        buffer = io.BytesIO()
        torch.jit.save(m, buffer, _extra_files=expected_extra_files)
        buffer.seek(0)
        extra_files = torch._C.ExtraFilesMap()
        extra_files['foo'] = ''
        torch.jit.load(buffer, _extra_files=extra_files)
        self.assertEqual('bar', extra_files['foo'])

        # Non-existent file 'bar'
        with self.assertRaises(RuntimeError):
            extra_files['bar'] = ''
            torch.jit.load(buffer, _extra_files=extra_files)


if __name__ == '__main__':
    raise RuntimeError("This test file is not meant to be run directly, use:\n\n"
                       "\tpython test/test_jit.py TESTNAME\n\n"
                       "instead.")

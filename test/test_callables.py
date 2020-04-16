from __future__ import division, absolute_import, print_function

__copyright__ = "Copyright (C) 2018 Kaushik Kulkarni"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import numpy as np
import pyopencl as cl
import pyopencl.clrandom  # noqa: F401
import loopy as lp
import pytest
import sys


from pyopencl.tools import (  # noqa: F401
        pytest_generate_tests_for_pyopencl
        as pytest_generate_tests)

from loopy.version import LOOPY_USE_LANGUAGE_VERSION_2018_2  # noqa: F401


def test_register_function_lookup(ctx_factory):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    from testlib import register_log2_lookup

    x = np.random.rand(10)
    ctx = cl.create_some_context()
    queue = cl.CommandQueue(ctx)

    prog = lp.make_kernel(
            "{[i]: 0<=i<10}",
            """
            y[i] = log2(x[i])
            """)
    prog = lp.register_function_id_to_in_knl_callable_mapper(prog,
            register_log2_lookup)

    evt, (out, ) = prog(queue, x=x)

    assert np.linalg.norm(np.log2(x)-out)/np.linalg.norm(np.log2(x)) < 1e-15


@pytest.mark.parametrize("inline", [False, True])
def test_register_knl(ctx_factory, inline):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)
    n = 2 ** 4

    x = np.random.rand(n, n, n, n, n)
    y = np.random.rand(n, n, n, n, n)

    grandchild_knl = lp.make_function(
            "{[i, j]:0<= i, j< 16}",
            """
            c[i, j] = 2*a[i, j] + 3*b[i, j]
            """, name='linear_combo1')

    child_knl = lp.make_function(
            "{[i, j]:0<=i, j < 16}",
            """
            [i, j]: g[i, j] = linear_combo1([i, j]: e[i, j], [i, j]: f[i, j])
            """, name='linear_combo2')

    parent_knl = lp.make_kernel(
            "{[i, j, k, l, m]: 0<=i, j, k, l, m<16}",
            """
            [j, l]: z[i, j, k, l, m] = linear_combo2([j, l]: x[i, j, k, l, m],
                                                     [j, l]: y[i, j, k, l, m])
            """,
            kernel_data=[
                lp.GlobalArg(
                    name='x',
                    dtype=np.float64,
                    shape=(16, 16, 16, 16, 16)),
                lp.GlobalArg(
                    name='y',
                    dtype=np.float64,
                    shape=(16, 16, 16, 16, 16)), '...'],
            )

    knl = lp.register_callable_kernel(
            parent_knl, child_knl)
    knl = lp.register_callable_kernel(
            knl, grandchild_knl)
    if inline:
        knl = lp.inline_callable_kernel(knl, 'linear_combo2')
        knl = lp.inline_callable_kernel(knl, 'linear_combo1')

    evt, (out, ) = knl(queue, x=x, y=y)

    assert (np.linalg.norm(2*x+3*y-out)/(
        np.linalg.norm(2*x+3*y))) < 1e-15


@pytest.mark.parametrize("inline", [False, True])
def test_slices_with_negative_step(ctx_factory, inline):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)
    n = 2 ** 4

    x = np.random.rand(n, n, n, n, n)
    y = np.random.rand(n, n, n, n, n)

    child_knl = lp.make_function(
            "{[i, j]:0<=i, j < 16}",
            """
            g[i, j] = 2*e[i, j] + 3*f[i, j]
            """, name="linear_combo")

    parent_knl = lp.make_kernel(
            "{[i, k, m]: 0<=i, k, m<16}",
            """
            z[i, 15:-1:-1, k, :, m] = linear_combo(x[i, :, k, :, m],
                                                   y[i, :, k, :, m])
            """,
            kernel_data=[
                lp.GlobalArg(
                    name='x',
                    dtype=np.float64,
                    shape=(16, 16, 16, 16, 16)),
                lp.GlobalArg(
                    name='y',
                    dtype=np.float64,
                    shape=(16, 16, 16, 16, 16)),
                lp.GlobalArg(
                    name='z',
                    dtype=np.float64,
                    shape=(16, 16, 16, 16, 16)), '...'],
            )

    knl = lp.register_callable_kernel(
            parent_knl, child_knl)
    if inline:
        knl = lp.inline_callable_kernel(knl, 'linear_combo')

    evt, (out, ) = knl(queue, x=x, y=y)

    assert (np.linalg.norm(2*x+3*y-out[:, ::-1, :, :, :])/(
        np.linalg.norm(2*x+3*y))) < 1e-15


@pytest.mark.parametrize("inline", [False, True])
def test_register_knl_with_call_with_kwargs(ctx_factory, inline):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    n = 2 ** 2

    a_dev = cl.clrandom.rand(queue, (n, n, n, n, n), np.float32)
    b_dev = cl.clrandom.rand(queue, (n, n, n, n, n), np.float32)
    c_dev = cl.clrandom.rand(queue, (n, n, n, n, n), np.float64)

    callee_knl = lp.make_function(
            "{[i, j]:0<=i, j < %d}" % n,
            """
            h[i, j] = 2 * e[i, j] + 3*f[i, j] + 4*g[i, j]
            <>f1[i, j] = 2*f[i, j]
            p[i, j] = 7 * e[i, j] + 4*f1[i, j] + 2*g[i, j]
            """,
            [
                lp.GlobalArg('f, e, h, g'), '...'],
            name='linear_combo')

    caller_knl = lp.make_kernel(
            "{[i, j, k, l, m]: 0<=i, j, k, l, m<%d}" % n,
            """
            <> d[i, j, k, l, m] = 2*b[i, j, k, l, m]
            [j, l]: x[i, j, k, l, m], [j, l]: y[i, j, k, l, m]  = linear_combo(
                                                     f=[j, l]: a[i, j, k, l, m],
                                                     g=[j, l]: d[i, j, k, l, m],
                                                     e=[j, l]: c[i, j, k, l, m])
            """)

    knl = lp.register_callable_kernel(
            caller_knl, callee_knl)
    if inline:
        knl = lp.inline_callable_kernel(knl, 'linear_combo')

    evt, (out1, out2, ) = knl(queue, a=a_dev, b=b_dev, c=c_dev)

    a = a_dev.get()
    b = b_dev.get()
    c = c_dev.get()

    h = out1.get()  # h = 2c + 3a +  8b
    p = out2.get()  # p = 7c + 8a + 4b
    h_exact = 3*a + 8*b + 2*c
    p_exact = 8*a + 4*b + 7*c

    assert np.linalg.norm(h-h_exact)/np.linalg.norm(h_exact) < 1e-7
    assert np.linalg.norm(p-p_exact)/np.linalg.norm(p_exact) < 1e-7


@pytest.mark.parametrize("inline", [False, True])
def test_register_knl_with_hw_axes(ctx_factory, inline):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    n = 2 ** 4

    x_dev = cl.clrandom.rand(queue, (n, n, n, n, n), np.float64)
    y_dev = cl.clrandom.rand(queue, (n, n, n, n, n), np.float64)

    callee_knl = lp.make_function(
            "{[i, j]:0<=i, j < 16}",
            """
            g[i, j] = 2*e[i, j] + 3*f[i, j]
            """, name='linear_combo')

    callee_knl = lp.split_iname(callee_knl, "i", 4, inner_tag="l.0", outer_tag="g.0")

    caller_knl = lp.make_kernel(
            "{[i, j, k, l, m]: 0<=i, j, k, l, m<16}",
            """
            [j, l]: z[i, j, k, l, m] = linear_combo([j, l]: x[i, j, k, l, m],
                                                     [j, l]: y[i, j, k, l, m])
            """
            )
    caller_knl = lp.split_iname(caller_knl, "i", 4, inner_tag="l.1", outer_tag="g.1")

    knl = lp.register_callable_kernel(
            caller_knl, callee_knl)

    if inline:
        knl = lp.inline_callable_kernel(knl, 'linear_combo')

    evt, (out, ) = knl(queue, x=x_dev, y=y_dev)

    x_host = x_dev.get()
    y_host = y_dev.get()

    assert np.linalg.norm(2*x_host+3*y_host-out.get())/np.linalg.norm(
            2*x_host+3*y_host) < 1e-15


@pytest.mark.parametrize("inline", [False, True])
def test_shape_translation_through_sub_array_ref(ctx_factory, inline):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    x1 = cl.clrandom.rand(queue, (3, 2), dtype=np.float64)
    x2 = cl.clrandom.rand(queue, (6, ), dtype=np.float64)
    x3 = cl.clrandom.rand(queue, (6, 6), dtype=np.float64)

    callee1 = lp.make_function(
            "{[i]: 0<=i<6}",
            """
            a[i] = 2*abs(b[i])
            """, name="callee_fn1")

    callee2 = lp.make_function(
            "{[i, j]: 0<=i<3 and 0 <= j < 2}",
            """
            a[i, j] = 3*b[i, j]
            """, name="callee_fn2")

    callee3 = lp.make_function(
            "{[i]: 0<=i<6}",
            """
            a[i] = 5*b[i]
            """, name="callee_fn3")

    knl = lp.make_kernel(
            "{[i, j, k, l]:  0<= i < 6 and 0 <= j < 3 and 0 <= k < 2 and 0<=l<6}",
            """
            [i]: y1[i//2, i%2] = callee_fn1([i]: x1[i//2, i%2])
            [j, k]: y2[2*j+k] = callee_fn2([j, k]: x2[2*j+k])
            [l]: y3[l, l] = callee_fn3([l]: x3[l, l])
            """)

    knl = lp.register_callable_kernel(knl, callee1)
    knl = lp.register_callable_kernel(knl, callee2)
    knl = lp.register_callable_kernel(knl, callee3)

    if inline:
        knl = lp.inline_callable_kernel(knl, 'callee_fn1')
        knl = lp.inline_callable_kernel(knl, 'callee_fn2')
        knl = lp.inline_callable_kernel(knl, 'callee_fn3')

    knl = lp.set_options(knl, "write_cl")
    knl = lp.set_options(knl, "return_dict")
    evt, out_dict = knl(queue, x1=x1, x2=x2, x3=x3)

    y1 = out_dict['y1'].get()
    y2 = out_dict['y2'].get()
    y3 = out_dict['y3'].get()

    assert (np.linalg.norm(y1-2*x1.get())) < 1e-15
    assert (np.linalg.norm(y2-3*x2.get())) < 1e-15
    assert (np.linalg.norm(np.diag(y3-5*x3.get()))) < 1e-15


def test_multi_arg_array_call(ctx_factory):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)
    import pymbolic.primitives as p
    n = 10
    acc_i = p.Variable("acc_i")[0]
    i = p.Variable("i")
    index = p.Variable("index")[0]
    a_i = p.Subscript(p.Variable("a"), p.Variable("i"))
    argmin_kernel = lp.make_function(
            "{[i]: 0 <= i < n}",
            [
                lp.Assignment(id="init2", assignee=index,
                    expression=0),
                lp.Assignment(id="init1", assignee=acc_i,
                    expression="214748367"),
                lp.Assignment(id="insn", assignee=index,
                    expression=p.If(p.Expression.eq(acc_i, a_i), i, index),
                    depends_on="update"),
                lp.Assignment(id="update", assignee=acc_i,
                    expression=p.Variable("min")(acc_i, a_i),
                    depends_on="init1,init2")],
            name="custom_argmin")

    argmin_kernel = lp.fix_parameters(argmin_kernel, n=n)

    knl = lp.make_kernel(
            "{[i]:0<=i<n}",
            """
            min_val, min_index = custom_argmin([i]:b[i])
            """)

    knl = lp.fix_parameters(knl, n=n)
    knl = lp.set_options(knl, return_dict=True)

    knl = lp.register_callable_kernel(knl, argmin_kernel)
    b = np.random.randn(n)
    evt, out_dict = knl(queue, b=b)
    tol = 1e-15
    from numpy.linalg import norm
    assert(norm(out_dict['min_val'][0] - np.min(b)) < tol)
    assert(norm(out_dict['min_index'][0] - np.argmin(b)) < tol)


@pytest.mark.parametrize("inline", [False, True])
def test_packing_unpacking(ctx_factory, inline):
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    x1 = cl.clrandom.rand(queue, (3, 2), dtype=np.float64)
    x2 = cl.clrandom.rand(queue, (6, ), dtype=np.float64)

    callee1 = lp.make_function(
            "{[i]: 0<=i<6}",
            """
            a[i] = 2*b[i]
            """, name="callee_fn1")

    callee2 = lp.make_function(
            "{[i, j]: 0<=i<2 and 0 <= j < 3}",
            """
            a[i, j] = 3*b[i, j]
            """, name="callee_fn2")

    knl = lp.make_kernel(
            "{[i, j, k]:  0<= i < 3 and 0 <= j < 2 and 0 <= k < 6}",
            """
            [i, j]: y1[i, j] = callee_fn1([i, j]: x1[i, j])
            [k]: y2[k] = callee_fn2([k]: x2[k])
            """)

    knl = lp.register_callable_kernel(knl, callee1)
    knl = lp.register_callable_kernel(knl, callee2)

    knl = lp.pack_and_unpack_args_for_call(knl, 'callee_fn1')
    knl = lp.pack_and_unpack_args_for_call(knl, 'callee_fn2')

    if inline:
        knl = lp.inline_callable_kernel(knl, 'callee_fn1')
        knl = lp.inline_callable_kernel(knl, 'callee_fn2')

    knl = lp.set_options(knl, "write_cl")
    knl = lp.set_options(knl, "return_dict")
    evt, out_dict = knl(queue, x1=x1, x2=x2)

    y1 = out_dict['y1'].get()
    y2 = out_dict['y2'].get()

    assert np.linalg.norm(2*x1.get()-y1)/np.linalg.norm(
            2*x1.get()) < 1e-15
    assert np.linalg.norm(3*x2.get()-y2)/np.linalg.norm(
            3*x2.get()) < 1e-15


def test_non_sub_array_refs_arguments(ctx_factory):
    import loopy as lp
    from loopy.transform.callable import _match_caller_callee_argument_dimension_

    callee = lp.make_function("{[i] : 0 <= i < 6}", "a[i] = a[i] + j",
            [lp.GlobalArg("a", dtype="double", shape=(6,), is_output_only=False),
                lp.ValueArg("j", dtype="int")], name="callee")
    caller1 = lp.make_kernel("{[j] : 0 <= j < 2}", "callee(a[:], b[0])",
            [lp.GlobalArg("a", dtype="double", shape=(6, ), is_output_only=False),
            lp.GlobalArg("b", dtype="double", shape=(1, ), is_output_only=False)],
            name="caller", target=lp.CTarget())

    caller2 = lp.make_kernel("{[j] : 0 <= j < 2}", "callee(a[:], 3.1415926)",
            [lp.GlobalArg("a", dtype="double", shape=(6, ),
                is_output_only=False)],
            name="caller", target=lp.CTarget())

    caller3 = lp.make_kernel("{[j] : 0 <= j < 2}", "callee(a[:], kappa)",
            [lp.GlobalArg("a", dtype="double", shape=(6, ),
                is_output_only=False)],
            name="caller", target=lp.CTarget())

    registered = lp.register_callable_kernel(caller1, callee)
    inlined = _match_caller_callee_argument_dimension_(registered, callee.name)
    inlined = lp.inline_callable_kernel(inlined, callee.name)

    print(inlined)

    registered = lp.register_callable_kernel(caller2, callee)
    inlined = _match_caller_callee_argument_dimension_(registered, callee.name)
    inlined = lp.inline_callable_kernel(inlined, callee.name)

    print(inlined)

    registered = lp.register_callable_kernel(caller3, callee)
    inlined = _match_caller_callee_argument_dimension_(registered, callee.name)
    inlined = lp.inline_callable_kernel(inlined, callee.name)

    print(inlined)


@pytest.mark.parametrize("inline", [False, True])
def test_empty_sub_array_refs(ctx_factory, inline):
    # See: https://github.com/OP2/PyOP2/pull/559#discussion_r272208618
    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)

    x = np.random.randn(10)
    y = np.random.randn(10)

    callee = lp.make_function(
            "{[d]:0<=d<1}",
            """
            a[d] = b[d] - c[d]

            """, name='wence_function')

    caller = lp.make_kernel("{[i]: 0<=i<10}",
            """
            []:z[i] = wence_function([]:x[i], []:y[i])
            """,
            [lp.GlobalArg('x, y', dtype=np.float64, shape=(10, )), '...'])

    caller = lp.register_callable_kernel(caller, callee)

    if inline:
        caller = lp.inline_callable_kernel(caller, callee.name)

    evt, (out, ) = caller(queue, x=x, y=y)
    assert np.allclose(out, x-y)


def test_nested_callable_inline():
    callee1 = lp.make_function(
            "{[i]: 0<=i<1}",
            """
            y[i] = pow(x[i], 2)
            """, name='callee1')
    callee2 = lp.make_kernel(
            "{[i]: 0<=i<1}",
            """
            []:y[i] = callee1([]: x[i])
            """, name='callee2')

    caller = lp.make_kernel("{[i]: 0<=i<10}",
                            """
                            []:z[i] = callee2([]:x[i])
                            """,
                            [lp.GlobalArg('x', dtype=float, shape=lp.auto),
                                '...'])

    callee2 = lp.register_callable_kernel(callee2, callee1)
    callee2 = lp.inline_callable_kernel(callee2, callee1.name)
    callee2 = callee2.root_kernel
    caller = lp.register_callable_kernel(caller, callee2)
    caller = lp.inline_callable_kernel(caller, callee2.name)
    print(caller)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from pytest import main
        main([__file__])

# vim: foldmethod=marker

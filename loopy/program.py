from __future__ import division, absolute_import

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

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

import six
import re

from pytools import ImmutableRecord, memoize_method
from pymbolic.primitives import Variable
from functools import wraps

from loopy.symbolic import (RuleAwareIdentityMapper, ResolvedFunction,
        CombineMapper, SubstitutionRuleExpander)
from loopy.kernel.function_interface import (
        CallableKernel, ScalarCallable)
from loopy.kernel.instruction import (
        MultiAssignmentBase, CInstruction, _DataObliviousInstruction)
from loopy.diagnostic import LoopyError
from loopy.library.reduction import ReductionOpFunction

from loopy.kernel import LoopKernel
from collections import Counter
from pymbolic.primitives import Call, CallWithKwargs

# FIXME: autofunction/autoclass?? ~KK


class ResolvedFunctionMarker(RuleAwareIdentityMapper):
    """
    Mapper to convert the  ``function`` attribute of a
    :class:`pymbolic.primitives.Call` known in the kernel as instances of
    :class:`loopy.symbolic.ResolvedFunction`. A function is known in the
    *kernel*, :func:`loopy.kernel.LoopKernel.find_scoped_function_identifier`
    returns an instance of
    :class:`loopy.kernel.function_interface.InKernelCallable`.

    **Example:** If given an expression of the form ``sin(x) + unknown_function(y) +
    log(z)``, then the mapper would return ``ResolvedFunction('sin')(x) +
    unknown_function(y) + ResolvedFunction('log')(z)``.

    :arg rule_mapping_context: An instance of
        :class:`loopy.symbolic.RuleMappingContext`.
    :arg function_ids: A container with instances of :class:`str` indicating
        the function identifiers to look for while scoping functions.
    """
    def __init__(self, rule_mapping_context, kernel, program_callables_info,
            function_id_to_in_knl_callable_mappers):
        super(ResolvedFunctionMarker, self).__init__(rule_mapping_context)
        self.kernel = kernel
        self.program_callables_info = program_callables_info
        self.function_id_to_in_knl_callable_mappers = (
                function_id_to_in_knl_callable_mappers)

    def find_in_knl_callable_from_identifier(self, identifier):
        """
        Returns an instance of
        :class:`loopy.kernel.function_interface.InKernelCallable` if the
        :arg:`identifier` is known to any kernel function scoper, otherwise returns
        *None*.
        """
        for func_id_to_in_knl_callable_mapper in (
                self.function_id_to_in_knl_callable_mappers):
            # fixme: do we really need to given target for the function
            in_knl_callable = func_id_to_in_knl_callable_mapper(
                    self.kernel.target, identifier)
            if in_knl_callable is not None:
                return in_knl_callable

        return None

    def map_call(self, expr, expn_state):
        from loopy.symbolic import parse_tagged_name

        name, tag = parse_tagged_name(expr.function)
        if name not in self.rule_mapping_context.old_subst_rules:
            new_call_with_kwargs = self.rec(CallWithKwargs(
                function=expr.function, parameters=expr.parameters,
                kw_parameters={}), expn_state)
            return Call(new_call_with_kwargs.function,
                    new_call_with_kwargs.parameters)
        else:
            return self.map_substitution(name, tag, expr.parameters, expn_state)

    def map_call_with_kwargs(self, expr, expn_state):

        if not isinstance(expr.function, ResolvedFunction):

            # search the kernel for the function.
            in_knl_callable = self.find_in_knl_callable_from_identifier(
                    expr.function.name)

            if in_knl_callable:
                # associate the newly created ResolvedFunction with the
                # resolved in-kernel callable

                self.program_callables_info, new_func_id = (
                        self.program_callables_info.with_add_callable(expr.function,
                            in_knl_callable))
                return type(expr)(
                        ResolvedFunction(new_func_id),
                        tuple(self.rec(child, expn_state)
                            for child in expr.parameters),
                        dict(
                            (key, self.rec(val, expn_state))
                            for key, val in six.iteritems(expr.kw_parameters))
                            )

        # this is an unknown function as of yet, do not modify it
        return super(ResolvedFunctionMarker, self).map_call_with_kwargs(expr,
                expn_state)

    def map_reduction(self, expr, expn_state):
        for func_id in (
                expr.operation.get_scalar_callables()):
            in_knl_callable = self.find_in_knl_callable_from_identifier(func_id)
            assert in_knl_callable is not None
            self.program_callables_info, _ = (
                    self.program_callables_info.with_add_callable(func_id,
                        in_knl_callable))
            # FIXME: where do you deal with the parameters? ~KK
        return super(ResolvedFunctionMarker, self).map_reduction(expr, expn_state)


def initialize_program_callables_info_from_kernel(kernel):
    """
    Returns an instance of :class:`loopy.ProgramCallablesInfo`, by resolving
    the functions based on :mod:`loopy`'s default function resolvers.
    """
    # collect the default function resolvers
    func_id_to_kernel_callable_mappers = (
            default_func_id_to_kernel_callable_mappers(kernel.target))
    program_callables_info = ProgramCallablesInfo({})

    from loopy.symbolic import SubstitutionRuleMappingContext
    rule_mapping_context = SubstitutionRuleMappingContext(
            kernel.substitutions, kernel.get_var_name_generator())

    resolved_function_marker = ResolvedFunctionMarker(
            rule_mapping_context, kernel, program_callables_info,
            func_id_to_kernel_callable_mappers)

    # mark the functions as "Resolved" in the expression nodes.
    kernel_with_functions_resolved = rule_mapping_context.finish_kernel(
            resolved_function_marker.map_kernel(kernel))
    # collect the update program_callables_info
    program_callables_info = resolved_function_marker.program_callables_info

    callable_kernel = CallableKernel(kernel_with_functions_resolved)

    # add the callable kernel to the program_callables_info
    program_callables_info, _ = program_callables_info.with_add_callable(
            Variable(kernel.name), callable_kernel)

    return program_callables_info


# {{{ program definition

class Program(ImmutableRecord):
    """
    Records the information about all the callables in a :mod:`loopy` program.

    .. attribute:: name

        An instance of :class:`str`, also the name of the top-most level
        :class:`loopy.LoopKernel`.

    .. attribute:: program_callables_info

        An instance of :class:`loopy.program.ProgramCallablesInfo`.

    .. attribute:: target

        An instance of :class:`loopy.target.TargetBase`.

    .. attribute:: func_id_to_in_knl_callables_mappers

        A list of functions of the signature ``(target: TargetBase,
        function_indentifier: str)`` that would return an instance of
        :class:`loopy.kernel.function_interface.InKernelCallable` or *None*.

    .. note::

        - To create an instance of :class:`loopy.Program`, it is recommeneded to
            go through :method:`loopy.make_kernel`.
        - This data structure and its attributes should be considered
          immutable, any modifications should be done through :method:`copy`.
    """
    def __init__(self,
            name,
            program_callables_info,
            target,
            func_id_to_in_knl_callable_mappers):
        assert isinstance(program_callables_info, ProgramCallablesInfo)

        assert name in program_callables_info

        super(Program, self).__init__(
                name=name,
                program_callables_info=program_callables_info,
                target=target,
                func_id_to_in_knl_callable_mappers=(
                    func_id_to_in_knl_callable_mappers))

        self._program_executor_cache = {}

    hash_fields = (
            "name",
            "program_callables_info",
            "target",)

    update_persistent_hash = LoopKernel.update_persistent_hash

    def copy(self, **kwargs):
        if 'target' in kwargs:
            # target attribute of all the callable kernels should be updated.
            target = kwargs['target']
            new_self = super(Program, self).copy(**kwargs)
            new_resolved_functions = {}
            for func_id, in_knl_callable in (
                    new_self.program_callables_info.items()):
                if isinstance(in_knl_callable, CallableKernel):
                    subkernel = in_knl_callable.subkernel
                    new_resolved_functions[func_id] = in_knl_callable.copy(
                            subkernel=subkernel.copy(target=target))
                else:
                    new_resolved_functions[func_id] = in_knl_callable

            program_callables_info = new_self.program_callables_info.copy(
                    resolved_functions=new_resolved_functions)

            return super(Program, new_self).copy(
                    program_callables_info=program_callables_info)
        else:
            return super(Program, self).copy(**kwargs)

    def get_grid_size_upper_bounds(self, ignore_auto=False):
        """Return a tuple (global_size, local_size) containing a grid that
        could accommodate execution of *all* instructions in the kernel.

        *global_size* and *local_size* are :class:`islpy.PwAff` objects.
        """
        return self.root_kernel.get_grid_size_upper_bounds(
                self.program_callables_info,
                ignore_auto=ignore_auto)

    def get_grid_size_upper_bounds_as_exprs(self, ignore_auto=False):
        """Return a tuple (global_size, local_size) containing a grid that
        could accommodate execution of *all* instructions in the kernel.

        *global_size* and *local_size* are :mod:`pymbolic` expressions
        """
        return self.root_kernel.get_grid_size_upper_bounds_as_exprs(
                self.program_callables_info,
                ignore_auto=ignore_auto)

    # {{{ implementation arguments

    @property
    @memoize_method
    def impl_arg_to_arg(self):
        from loopy.kernel.array import ArrayBase

        result = {}

        for arg in self.args:
            if not isinstance(arg, ArrayBase):
                result[arg.name] = arg
                continue

            if arg.shape is None or arg.dim_tags is None:
                result[arg.name] = arg
                continue

            subscripts_and_names = arg.subscripts_and_names()
            if subscripts_and_names is None:
                result[arg.name] = arg
                continue

            for index, sub_arg_name in subscripts_and_names:
                result[sub_arg_name] = arg

        return result

    # }}}

    @property
    def root_kernel(self):
        """
        Returns an instance of :class:`loopy.LoopKernel` denoting the topmost
        level kernel in codegeneration.

        .. note::

            Syntactic sugar.
        """
        return self.program_callables_info[self.name].subkernel

    @property
    def arg_dict(self):
        """
        Returns ``arg_dict`` of the ``root_kernel``.

        .. note::

            Syntactic sugar.
        """
        return self.root_kernel.arg_dict

    @property
    def args(self):
        """
        Returns ``args`` of the ``root_kernel``.

        .. note::

            Syntactic sugar.
        """
        return self.root_kernel.args[:]

    def with_root_kernel(self, root_kernel):
        """
        Returns a copy of *self* with the topmost level kernel as
        *root_kernel*.
        """
        new_in_knl_callable = self.program_callables_info[
                self.name].copy(subkernel=root_kernel)
        new_resolved_functions = (
                self.program_callables_info.resolved_functions.copy())
        new_resolved_functions[self.name] = new_in_knl_callable

        return self.copy(
                program_callables_info=self.program_callables_info.copy(
                    resolved_functions=new_resolved_functions))

    def __call__(self, *args, **kwargs):
        key = self.target.get_kernel_executor_cache_key(*args, **kwargs)
        try:
            pex = self._program_executor_cache[key]
        except KeyError:
            pex = self.target.get_kernel_executor(self, *args, **kwargs)
            self._program_executor_cache[key] = pex

        return pex(*args, **kwargs)

    def __str__(self):
        return self.root_kernel.__str__()

# }}}


def next_indexed_function_identifier(function_id):
    """
    Returns an instance of :class:`str` with the next indexed-name in the
    sequence for the name of *function*.

    *Example:* ``'sin_0'`` will return ``'sin_1'``.

    :arg function_id: Either an instance of :class:`str`.
    """

    # {{{ sanity checks

    assert isinstance(function_id, str)

    # }}}

    func_name = re.compile(r"^(?P<alpha>\S+?)_(?P<num>\d+?)$")

    match = func_name.match(function_id)

    if match is None:
        if function_id[-1] == '_':
            return "{old_name}0".format(old_name=function_id)
        else:
            return "{old_name}_0".format(old_name=function_id)

    return "{alpha}_{num}".format(alpha=match.group('alpha'),
            num=int(match.group('num'))+1)


class ResolvedFunctionRenamer(RuleAwareIdentityMapper):
    """
    Mapper to rename the resolved functions in an expression according to
    *renaming_dict*.
    """
    def __init__(self, rule_mapping_context, renaming_dict):
        super(ResolvedFunctionRenamer, self).__init__(
                rule_mapping_context)
        self.renaming_dict = renaming_dict

    def map_resolved_function(self, expr, expn_state):
        if expr.name in self.renaming_dict:
            return ResolvedFunction(self.renaming_dict[expr.name])
        else:
            return super(ResolvedFunctionRenamer, self).map_resolved_function(
                    expr, expn_state)


def rename_resolved_functions_in_a_single_kernel(kernel,
        renaming_dict):
    """
    Returns a copy of *kernel* with the instances of :class:`ResolvedFunction`
    renames according to *renaming_dict*.
    """
    from loopy.symbolic import SubstitutionRuleMappingContext
    rule_mapping_context = SubstitutionRuleMappingContext(
                kernel.substitutions, kernel.get_var_name_generator())
    resolved_function_renamer = ResolvedFunctionRenamer(rule_mapping_context,
            renaming_dict)
    return (
            rule_mapping_context.finish_kernel(
                resolved_function_renamer.map_kernel(kernel)))


# {{{ counting helpers

class CallablesCountingMapper(CombineMapper):
    """
    Returns an instance of :class:`collections.Counter` with the count of
    callables registered in *program_callables_info*.

    .. attribute:: program_callables_info

        An instance of :class:`loopy.program.ProgramCallablesInfo`.
    """
    def __init__(self, program_callables_info):
        self.program_callables_info = program_callables_info

    def combine(self, values):
        return sum(values, Counter())

    def map_call(self, expr):

        if isinstance(expr, CallWithKwargs):
            kw_parameters = expr.kw_parameters
        else:
            assert isinstance(expr, Call)
            kw_parameters = {}

        if isinstance(expr.function, (ResolvedFunction)):
            in_knl_callable = self.program_callables_info[expr.function.name]
            if isinstance(in_knl_callable, ScalarCallable):
                return (Counter([expr.function.name]) +
                        self.combine((self.rec(child) for child in expr.parameters
                            + tuple(kw_parameters.values()))))

            elif isinstance(in_knl_callable, CallableKernel):

                # callable kernels have more callables in them.
                callables_count_in_subkernel = (
                        count_callables_in_kernel(
                            in_knl_callable.subkernel,
                            self.program_callables_info))

                return (Counter([expr.function.name]) +
                        self.combine((self.rec(child) for child in expr.parameters
                            + tuple(kw_parameters.values())))) + (
                                    callables_count_in_subkernel)
            else:
                raise NotImplementedError("Unknown callable type %s." % (
                    type))
        else:
            return (
                    self.combine((self.rec(child) for child in expr.parameters
                        + tuple(kw_parameters.values()))))

    map_call_with_kwargs = map_call

    def map_reduction(self, expr):
        return Counter(expr.operation.get_scalar_callables()) + (
                super(CallablesCountingMapper, self).map_reduction(expr))

    def map_constant(self, expr):
        return Counter()

    map_variable = map_constant
    map_function_symbol = map_constant
    map_tagged_variable = map_constant
    map_type_cast = map_constant


@memoize_method
def count_callables_in_kernel(kernel, program_callables_info):
    """
    Returns an instance of :class:`collections.Counter` representing the number
    of callables in the *kernel* that are registered in
    *program_callables_info*.
    """
    assert isinstance(kernel, LoopKernel)
    callables_count = Counter()
    callables_counting_mapper = CallablesCountingMapper(
            program_callables_info)
    subst_expander = SubstitutionRuleExpander(kernel.substitutions)

    for insn in kernel.instructions:
        if isinstance(insn, MultiAssignmentBase):
            callables_count += (
                    callables_counting_mapper(subst_expander(
                        insn.expression)))
        elif isinstance(insn, (_DataObliviousInstruction, CInstruction)):
            pass
        else:
            raise NotImplementedError("Unknown instruction type %s." % (
                type(insn)))

    return callables_count

# }}}


# {{{ program callables info

class ProgramCallablesInfo(ImmutableRecord):
    """
    Records the information of all the callables called in a :class:`loopy.Program`.

    .. attribute:: resolved_functions

        An instance of :class:`dict` that contains a mapping from function
        identifier to instances of
        :class:`loopy.kernel.function_interface.InKernelCallable`

    .. attribute:: history

        An instance of :class:`dict` that contains a mapping from function
        identifier to and instance of :class:`list`that would contain all the
        names taken by a function before the current name.(For example: one
        possibility could be ``{'sin_1': ['sin', 'sin_0', 'sin_1']}``)

    .. attribute:: is_being_edited

        An instance of :class:`bool` which is intended to aid the working of
        :meth:`with_enter_edit_callables_mode`, :meth:`with_callable` and
        :meth:`with_exit_edit_callables_mode`.
    """
    def __init__(self, resolved_functions,
            history=None, is_being_edited=False):

        if history is None:
            history = dict((func_id, frozenset([func_id])) for func_id in
                    resolved_functions)

        super(ProgramCallablesInfo, self).__init__(
                resolved_functions=resolved_functions,
                history=history,
                is_being_edited=is_being_edited)

    hash_fields = (
            "resolved_functions",
            "is_being_edited",
            "history")

    def __hash__(self):
        return hash((
            frozenset(six.iteritems(self.resolved_functions)),
            frozenset(six.iteritems(self.history)),
            self.is_being_edited
            ))

    update_persistent_hash = LoopKernel.update_persistent_hash

    @memoize_method
    def callables_count(self):
        """
        Returns an instance of :class:`collection.Counter` representing the number
        of times the callables is called in program_callables_info.
        """
        # should raise an error if there are more than  one root kernels(which is
        # illegal)
        root_kernel_name, = [in_knl_callable.subkernel.name for in_knl_callable
                in self.values() if
                isinstance(in_knl_callable, CallableKernel) and
                in_knl_callable.subkernel.is_called_from_host]

        from collections import Counter
        callables_count = Counter([root_kernel_name])
        callables_count += (
                count_callables_in_kernel(self[
                    root_kernel_name].subkernel, self))

        return callables_count

    # {{{ interface to perfrom edits on callables

    def with_add_callable(self, function, in_kernel_callable):
        """
        Returns a copy of *self* with the *function* associated with the
        *in_kernel_callable*.
        """
        # FIXME: pleasse better docs.. ~KK
        # note: this does not require the edit mode to be true.
        # the reason for the edit mode is that we need to take care of the
        # renaming that might be needed to be done
        # PS: delete this note?

        # {{{ sanity checks

        if isinstance(function, str):
            function = Variable(function)

        assert isinstance(function, (Variable, ReductionOpFunction))

        # }}}

        history = self.history.copy()

        if in_kernel_callable in self.resolved_functions.values():
            # the callable already exists, implies return the function
            # identifier corresposing to that callable.
            for func_id, in_knl_callable in self.resolved_functions.items():
                if in_knl_callable == in_kernel_callable:
                    history[func_id] = history[func_id] | frozenset([function.name])
                    return (
                            self.copy(
                                history=history),
                            func_id)
        else:

            # {{{ handle ReductionOpFunction

            if isinstance(function, ReductionOpFunction):
                unique_function_identifier = function.copy()
                updated_resolved_functions = self.resolved_functions.copy()
                updated_resolved_functions[unique_function_identifier] = (
                        in_kernel_callable)
                history[unique_function_identifier] = frozenset(
                        [unique_function_identifier])

                return (
                        self.copy(
                            history=history,
                            resolved_functions=updated_resolved_functions),
                        unique_function_identifier)

            # }}}

            unique_function_identifier = function.name

            if isinstance(in_kernel_callable, CallableKernel) and (
                    in_kernel_callable.subkernel.is_called_from_host):
                # special treatment if the callable is the root kernel
                pass
            else:
                while unique_function_identifier in self.resolved_functions:
                    unique_function_identifier = (
                            next_indexed_function_identifier(
                                unique_function_identifier))

        updated_resolved_functions = self.resolved_functions.copy()
        updated_resolved_functions[unique_function_identifier] = (
                in_kernel_callable)

        if 'strongVolumeKernelR_0' in updated_resolved_functions:
            import pudb
            pudb.set_trace()

        history[unique_function_identifier] = frozenset(
                [unique_function_identifier])

        return (
                self.copy(
                    history=history,
                    resolved_functions=updated_resolved_functions),
                Variable(unique_function_identifier))

    def with_edit_callables_mode(self):
        """
        Initiates *self* for a walk traversal through all the callables.
        """
        # PS: I don't see a need for this method right now.
        # This is just for validation purposes, maybe needs to disapper if you
        # find a better solution?
        return self.copy(
                is_being_edited=True)

    def with_callable(self, function, in_kernel_callable):
        """
        :arg function: An instance of :class:`pymbolic.primitives.Variable` or
            :class:`loopy.library.reduction.ReductionOpFunction`.

        :arg in_kernel_callables: An instance of
            :class:`loopy.InKernelCallable`.

        .. note::

            - Use :meth:`with_add_callable` if a callable is being resolved for the
                first time.
        """

        # {{{ non-edit mode

        if not self.is_being_edited:
            if function.name in self.resolved_functions and (
                    self.resolved_functions[function.name] == in_kernel_callable):
                # if not being edited, check that the given function is
                # equal to the the old version of the callable.
                return self, function
            else:
                print('Old: ', self.resolved_functions[function.name])
                print('New: ', in_kernel_callable)
                raise LoopyError("Use 'with_enter_edit_callables_mode' first.")

        # }}}

        # {{{ sanity checks

        if isinstance(function, str):
            function = Variable(function)

        assert isinstance(function, (Variable, ReductionOpFunction))

        # }}}

        history = self.history.copy()

        if in_kernel_callable in self.resolved_functions.values():

            # the callable already exists, hence return the function
            # identifier corresponding to that callable.
            for func_id, in_knl_callable in self.resolved_functions.items():
                if in_knl_callable == in_kernel_callable:
                    history[func_id] = history[func_id] | frozenset([function.name])
                    return (
                            self.copy(
                                history=history),
                            func_id)
        else:
            # {{{ handle ReductionOpFunction

            if isinstance(function, ReductionOpFunction):
                unique_function_identifier = function.copy()
                updated_resolved_functions = self.resolved_functions.copy()
                updated_resolved_functions[unique_function_identifier] = (
                        in_kernel_callable)

                return (
                        self.copy(
                            resolved_functions=updated_resolved_functions),
                        unique_function_identifier)

            # }}}
            unique_function_identifier = function.name

            if isinstance(in_kernel_callable, CallableKernel) and (
                    in_kernel_callable.subkernel.is_called_from_host):
                # special treatment if the callable is the root kernel
                pass
            else:
                while unique_function_identifier in self.resolved_functions:
                    unique_function_identifier = (
                            next_indexed_function_identifier(
                                unique_function_identifier))

        updated_resolved_functions = self.resolved_functions.copy()
        updated_resolved_functions[unique_function_identifier] = (
                in_kernel_callable)

        if 'strongVolumeKernelR_0' in updated_resolved_functions:
            import pudb
            pudb.set_trace()

        history[unique_function_identifier] = (
                history[function.name] | frozenset([unique_function_identifier]))

        return (
                self.copy(
                    history=history,
                    resolved_functions=updated_resolved_functions),
                Variable(unique_function_identifier))

    def with_exit_edit_callables_mode(self, old_callables_count):
        """
        Returns a copy of *self* with renaming of the callables done whenver
        possible.

        *For example: * If all the ``sin`` got diverged as ``sin_0, sin_1``,
        then all the renaming is done such that one of flavors of the function
        is renamed back to ``sin``.
        """

        new_callables_count = self.callables_count()
        history = self.history.copy()
        renames_needed = {}

        assert self.is_being_edited

        # NOTE:(to self by KK)
        # all we need to do is change the name of the variables that were seen
        # in old_callables_count but are no longer available.
        # Using these 2 figure out the renames needed.
        for old_func_id in old_callables_count-new_callables_count:
            # this implies that all the function instances having the name
            # "func_id" have been renamed to something else.
            for new_func_id in (
                    six.viewkeys(new_callables_count)-six.viewkeys(renames_needed)):
                if old_func_id in history[new_func_id]:
                    renames_needed[new_func_id] = old_func_id
                    break

        resolved_functions = {}

        for func_id in new_callables_count:
            in_knl_callable = self.resolved_functions[func_id]
            if isinstance(in_knl_callable, CallableKernel):
                # If callable kernel, perform renames.
                old_subkernel = in_knl_callable.subkernel
                new_subkernel = rename_resolved_functions_in_a_single_kernel(
                        old_subkernel, renames_needed)
                in_knl_callable = (
                        in_knl_callable.copy(subkernel=new_subkernel))
            elif isinstance(in_knl_callable, ScalarCallable):
                pass
            else:
                raise NotImplementedError("Unknown callable type %s." %
                        type(in_knl_callable).__name__)

            if func_id in renames_needed:
                # If function name itself in renames change the key of the
                # dict.
                history.pop(func_id)

                new_func_id = renames_needed[func_id]
                resolved_functions[new_func_id] = (
                        in_knl_callable)
            else:
                resolved_functions[func_id] = in_knl_callable

        return self.copy(
                is_being_edited=False,
                resolved_functions=resolved_functions)

    # }}}

    # {{{ behave like a dict(syntactic sugar)

    def __getitem__(self, item):
        return self.resolved_functions[item]

    def __contains__(self, item):
        return item in self.resolved_functions

    def items(self):
        return self.resolved_functions.items()

    def values(self):
        return self.resolved_functions.values()

    # }}}

# }}}


def default_func_id_to_kernel_callable_mappers(target):
    """
    Returns a list of functions that are provided through *target* by deafault.
    """
    # FIXME: name scopers is confusing!(change it to something else.)

    from loopy.library.function import loopy_specific_callable_scopers
    return (
            [loopy_specific_callable_scopers] + (
                target.get_device_ast_builder().function_scopers()))


# {{{ helper functions

def make_program_from_kernel(kernel):
    """
    Returns an instance of :class:`loopy.Program` with the *kernel* as the root
    kernel.
    """

    # get the program callables info
    program_callables_info = initialize_program_callables_info_from_kernel(kernel)

    # get the program from program callables info
    program = Program(
            name=kernel.name,
            program_callables_info=program_callables_info,
            func_id_to_in_knl_callable_mappers=(
                default_func_id_to_kernel_callable_mappers(kernel.target)),
            target=kernel.target)

    return program


def iterate_over_kernels_if_given_program(transform_for_single_kernel):
    """
    Function wrapper for transformations of the type ``transform(kernel:
    LoopKernel, *args, **kwargs): LoopKernel``. Returns a function with the
    ``transform`` being implemented on all of the callable kernels in a
    :class:`loopy.Program`.
    """
    def _collective_transform(program_or_kernel, *args, **kwargs):
        if isinstance(program_or_kernel, Program):
            program = program_or_kernel
            new_resolved_functions = {}
            for func_id, in_knl_callable in program.program_callables_info.items():
                if isinstance(in_knl_callable, CallableKernel):
                    new_subkernel = transform_for_single_kernel(
                            in_knl_callable.subkernel, *args, **kwargs)
                    in_knl_callable = in_knl_callable.copy(
                            subkernel=new_subkernel)

                elif isinstance(in_knl_callable, ScalarCallable):
                    pass
                else:
                    raise NotImplementedError("Unknown type of callable %s." % (
                        type(in_knl_callable).__name__))

                new_resolved_functions[func_id] = in_knl_callable

            new_program_callables_info = program.program_callables_info.copy(
                    resolved_functions=new_resolved_functions)
            return program.copy(program_callables_info=new_program_callables_info)
        else:
            assert isinstance(program_or_kernel, LoopKernel)
            kernel = program_or_kernel
            return transform_for_single_kernel(kernel, *args, **kwargs)

    return wraps(transform_for_single_kernel)(_collective_transform)

# }}}


# vim: foldmethod=marker

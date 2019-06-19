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

import six  # noqa

from loopy.diagnostic import LoopyError
from loopy.kernel import LoopKernel
from loopy.kernel.function_interface import (ScalarCallable, CallableKernel)
from loopy.program import Program, iterate_over_kernels_if_given_program
from loopy.tools import natsorted
from pymbolic.primitives import (Variable, Subscript)
from loopy.kernel.instruction import MultiAssignmentBase


# {{{ find_instructions

def find_instructions_in_single_kernel(kernel, insn_match):
    assert isinstance(kernel, LoopKernel)
    from loopy.match import parse_match
    match = parse_match(insn_match)
    return [insn for insn in kernel.instructions if match(kernel, insn)]


def find_instructions(program, insn_match):
    assert isinstance(program, Program)
    insns = []
    for in_knl_callable in program.callables_table.values():
        if isinstance(in_knl_callable, CallableKernel):
            insns += (find_instructions_in_single_kernel(
                in_knl_callable.subkernel, insn_match))
        elif isinstance(in_knl_callable, ScalarCallable):
            pass
        else:
            raise NotImplementedError("Unknown callable type %s." % (
                type(in_knl_callable)))

    return insns

# }}}


# {{{ map_instructions

def map_instructions(kernel, insn_match, f):
    from loopy.match import parse_match
    match = parse_match(insn_match)

    new_insns = []

    for insn in kernel.instructions:
        if match(kernel, insn):
            new_insns.append(f(insn))
        else:
            new_insns.append(insn)

    return kernel.copy(instructions=new_insns)

# }}}


# {{{ set_instruction_priority

@iterate_over_kernels_if_given_program
def set_instruction_priority(kernel, insn_match, priority):
    """Set the priority of instructions matching *insn_match* to *priority*.

    *insn_match* may be any instruction id match understood by
    :func:`loopy.match.parse_match`.
    """

    def set_prio(insn):
        return insn.copy(priority=priority)

    return map_instructions(kernel, insn_match, set_prio)

# }}}


# {{{ add_dependency

@iterate_over_kernels_if_given_program
def add_dependency(kernel, insn_match, depends_on):
    """Add the instruction dependency *dependency* to the instructions matched
    by *insn_match*.

    *insn_match* and *depends_on* may be any instruction id match understood by
    :func:`loopy.match.parse_match`.

    .. versionchanged:: 2016.3

        Third argument renamed to *depends_on* for clarity, allowed to
        be not just ID but also match expression.
    """

    if isinstance(depends_on, str) and depends_on in kernel.id_to_insn:
        added_deps = frozenset([depends_on])
    else:
        added_deps = frozenset(
                dep.id for dep in find_instructions_in_single_kernel(kernel,
                    depends_on))

    if not added_deps:
        raise LoopyError("no instructions found matching '%s' "
                "(to add as dependencies)" % depends_on)

    matched = [False]

    def add_dep(insn):
        new_deps = insn.depends_on
        matched[0] = True
        if new_deps is None:
            new_deps = added_deps
        else:
            new_deps = new_deps | added_deps

        return insn.copy(depends_on=new_deps)

    result = map_instructions(kernel, insn_match, add_dep)

    if not matched[0]:
        raise LoopyError("no instructions found matching '%s' "
                "(to which dependencies would be added)" % insn_match)

    return result

# }}}


# {{{ remove_dependency

@iterate_over_kernels_if_given_program
def remove_dependency(kernel, insn_match, depends_on):
    """Remove the instruction dependency *dependency* to the instructions matched
    by *insn_match*.

    *insn_match* and *depends_on* may be any instruction id match understood by
    :func:`loopy.match.parse_match`.
    """

    #FIXME: @inducer: should we somehow unify the logic of remove_dependency
    # and add_dependency. As there is substantial logic overlap.

    if isinstance(depends_on, str) and depends_on in kernel.id_to_insn:
        remove_deps = frozenset([depends_on])
    else:
        remove_deps = frozenset(
                dep.id for dep in find_instructions_in_single_kernel(kernel,
                    depends_on))

    if not remove_deps:
        raise LoopyError("no instructions found matching '%s' "
                "(to remove as dependencies)" % depends_on)

    matched = [False]

    def remove_dep(insn):
        new_deps = insn.depends_on
        matched[0] = True
        if new_deps is None:
            new_deps = None
        else:
            new_deps = new_deps - remove_deps

        return insn.copy(depends_on=new_deps)

    result = map_instructions(kernel, insn_match, remove_dep)

    if not matched[0]:
        raise LoopyError("no instructions found matching '%s' "
                "(to which dependencies would be removed)" % insn_match)

    return result

# }}}


# {{{ remove_instructions

def remove_instructions(kernel, insn_ids):
    """Return a new kernel with instructions in *insn_ids* removed.

    Dependencies across (one, for now) deleted isntructions are propagated.
    Behavior is undefined for now for chains of dependencies within the
    set of deleted instructions.

    This also updates *no_sync_with* for all instructions.
    """

    if not insn_ids:
        return kernel

    if isinstance(insn_ids, str):
        from loopy.match import parse_match
        try:
            within = parse_match(insn_ids)
        except LoopyError:
            raise LoopyError("insn_ids should be either set or a str as "
                    "understood by *loopy.match.parse_match*")

        insn_ids = set([insn.id for insn in kernel.instructions if
            within(kernel, insn)])

    assert isinstance(insn_ids, set)
    id_to_insn = kernel.id_to_insn

    new_insns = []
    for insn in kernel.instructions:
        if insn.id in insn_ids:
            continue

        # transitively propagate dependencies
        # (only one level for now)
        if insn.depends_on is None:
            depends_on = frozenset()
        else:
            depends_on = insn.depends_on

        new_deps = depends_on.copy()

        for dep_id in depends_on & insn_ids:
            new_deps = new_deps | id_to_insn[dep_id].depends_on

        new_deps = new_deps - insn_ids

        # update no_sync_with

        new_no_sync_with = frozenset((insn_id, scope)
                for insn_id, scope in insn.no_sync_with
                if insn_id not in insn_ids)

        new_insns.append(
                insn.copy(depends_on=new_deps, no_sync_with=new_no_sync_with))

    return kernel.copy(
            instructions=new_insns)

# }}}


# {{{ replace_instruction_ids

def replace_instruction_ids(kernel, replacements):
    new_insns = []

    for insn in kernel.instructions:
        changed = False
        new_depends_on = []
        new_no_sync_with = []

        for dep in insn.depends_on:
            if dep in replacements:
                new_depends_on.extend(replacements[dep])
                changed = True
            else:
                new_depends_on.append(dep)

        for insn_id, scope in insn.no_sync_with:
            if insn_id in replacements:
                new_no_sync_with.extend(
                        (repl, scope) for repl in replacements[insn_id])
                changed = True
            else:
                new_no_sync_with.append((insn_id, scope))

        new_insns.append(
                insn.copy(
                    depends_on=frozenset(new_depends_on),
                    no_sync_with=frozenset(new_no_sync_with))
                if changed else insn)

    return kernel.copy(instructions=new_insns)

# }}}


# {{{ tag_instructions

@iterate_over_kernels_if_given_program
def tag_instructions(kernel, new_tag, within=None):
    from loopy.match import parse_match
    within = parse_match(within)

    new_insns = []
    for insn in kernel.instructions:
        if within(kernel, insn):
            new_insns.append(
                    insn.copy(tags=insn.tags | frozenset([new_tag])))
        else:
            new_insns.append(insn)

    return kernel.copy(instructions=new_insns)

# }}}


# {{{ add nosync

@iterate_over_kernels_if_given_program
def add_nosync(kernel, scope, source, sink, bidirectional=False, force=False,
        empty_ok=False):
    """Add a *no_sync_with* directive between *source* and *sink*.
    *no_sync_with* is only added if *sink* depends on *source* or
    if the instruction pair is in a conflicting group.

    This function does not check for the presence of a memory dependency.

    :arg kernel: The kernel
    :arg source: Either a single instruction id, or any instruction id
        match understood by :func:`loopy.match.parse_match`.
    :arg sink: Either a single instruction id, or any instruction id
        match understood by :func:`loopy.match.parse_match`.
    :arg scope: A valid *no_sync_with* scope. See
        :attr:`loopy.InstructionBase.no_sync_with` for allowable scopes.
    :arg bidirectional: A :class:`bool`. If *True*, add a *no_sync_with*
        to both the source and sink instructions, otherwise the directive
        is only added to the sink instructions.
    :arg force: A :class:`bool`. If *True*, add a *no_sync_with* directive
        even without the presence of a dependency edge or conflicting
        instruction group.
    :arg empty_ok: If *True*, do not complain even if no *nosync* tags were
        added as a result of the transformation.

    :return: The updated kernel

    .. versionchanged:: 2018.1

        If the transformation adds no *nosync* directives, it will complain.
        This used to silently pass. This behavior can be restored using
        *empty_ok*.
    """
    assert isinstance(kernel, LoopKernel)

    if isinstance(source, str) and source in kernel.id_to_insn:
        sources = frozenset([source])
    else:
        sources = frozenset(
                source.id for source in find_instructions_in_single_kernel(
                    kernel, source))

    if isinstance(sink, str) and sink in kernel.id_to_insn:
        sinks = frozenset([sink])
    else:
        sinks = frozenset(
                sink.id for sink in find_instructions_in_single_kernel(
                    kernel, sink))

    if not sources and not empty_ok:
        raise LoopyError("No match found for source specification '%s'." % source)
    if not sinks and not empty_ok:
        raise LoopyError("No match found for sink specification '%s'." % sink)

    def insns_in_conflicting_groups(insn1_id, insn2_id):
        insn1 = kernel.id_to_insn[insn1_id]
        insn2 = kernel.id_to_insn[insn2_id]
        return (
                bool(insn1.groups & insn2.conflicts_with_groups)
                or
                bool(insn2.groups & insn1.conflicts_with_groups))

    from collections import defaultdict
    nosync_to_add = defaultdict(set)

    rec_dep_map = kernel.recursive_insn_dep_map()
    for sink in sinks:
        for source in sources:

            needs_nosync = force or (
                    source in rec_dep_map[sink]
                    or insns_in_conflicting_groups(source, sink))

            if not needs_nosync:
                continue

            nosync_to_add[sink].add((source, scope))
            if bidirectional:
                nosync_to_add[source].add((sink, scope))

    if not nosync_to_add and not empty_ok:
        raise LoopyError("No nosync annotations were added as a result "
                "of this call. add_nosync will (by default) only add them to "
                "accompany existing depencies or group exclusions. Maybe you want "
                "to pass force=True?")

    new_instructions = list(kernel.instructions)

    for i, insn in enumerate(new_instructions):
        if insn.id in nosync_to_add:
            new_instructions[i] = insn.copy(no_sync_with=insn.no_sync_with
                    | frozenset(nosync_to_add[insn.id]))

    return kernel.copy(instructions=new_instructions)

# }}}


# {{{ uniquify_instruction_ids

@iterate_over_kernels_if_given_program
def uniquify_instruction_ids(kernel):
    """Converts any ids that are :class:`loopy.UniqueName` or *None* into unique
    strings.

    This function does *not* deduplicate existing instruction ids.
    """

    from loopy.kernel.creation import UniqueName

    insn_ids = set(
            insn.id for insn in kernel.instructions
            if insn.id is not None and not isinstance(insn.id, UniqueName))

    from pytools import UniqueNameGenerator
    insn_id_gen = UniqueNameGenerator(insn_ids)

    new_instructions = []

    for insn in kernel.instructions:
        if insn.id is None:
            new_instructions.append(
                    insn.copy(id=insn_id_gen("insn")))
        elif isinstance(insn.id, UniqueName):
            new_instructions.append(
                    insn.copy(id=insn_id_gen(insn.id.name)))
        else:
            new_instructions.append(insn)

    return kernel.copy(instructions=new_instructions)

# }}}


def remove_unnecessary_deps(kernel):

    ordered_insn_ids = set()
    insn_order = []

    def insert_insn_into_order(insn):
        if insn.id in ordered_insn_ids:
            return
        ordered_insn_ids.add(insn.id)

        for dep_id in natsorted(insn.depends_on):
            insert_insn_into_order(kernel.id_to_insn[dep_id])

        insn_order.append(insn)

    for insn in kernel.instructions:
        insert_insn_into_order(insn)

    new_insns = insn_order.copy()

    for i, source_insn in enumerate(insn_order):
        if isinstance(source_insn, MultiAssignmentBase):
            written_var = source_insn.assignee
            if isinstance(written_var, Variable):
                written_var_name = written_var.name
            else:
                assert isinstance(written_var, Subscript)
                written_var_name = written_var.aggregate.name

            for j, sink_insn in enumerate(insn_order[i+1:]):
                if written_var_name in sink_insn.read_dependency_names():
                    assert new_insns[j+i+1].id == sink_insn.id
                    new_insns[j+1+i] = new_insns[j+1+i].copy(
                            depends_on=(new_insns[j+1+i].depends_on
                                | frozenset([source_insn.id])))
                else:
                    assert new_insns[j+i+1].id == sink_insn.id
                    new_insns[j+1+i] = new_insns[j+1+i].copy(
                            depends_on=(new_insns[j+1+i].depends_on
                                - frozenset([source_insn.id])))

    return kernel.copy(instructions=new_insns)


# vim: foldmethod=marker

from pymbolic.primitives import Variable, Subscript
from loopy.symbolic import (RuleAwareIdentityMapper, SubstitutionRuleMappingContext)
from loopy.transform.iname import remove_unused_inames


class AxisRemover(RuleAwareIdentityMapper):
    def __init__(self, rule_mapping_context, var_name, axis_num):
        self.var_name = var_name
        self.axis_num = axis_num
        super(AxisRemover, self).__init__(rule_mapping_context)

    def map_subscript(self, expr, expn_state):
        if expr.aggregate.name == self.var_name:
            if len(expr.index_tuple) == 1:
                return Variable(self.var_name)
            else:
                return Subscript(expr.aggregate,
                        expr.index_tuple[:self.axis_num]
                        + expr.index_tuple[self.axis_num+1:])

        return super(AxisRemover, self).map_subscript(expr, expn_state)


def remove_axis(kernel, var_name, axis_num):
    assert var_name in kernel.temporary_variables

    assert axis_num < len(kernel.temporary_variables[var_name].shape)

    rule_mapping_context = SubstitutionRuleMappingContext(kernel.substitutions,
            kernel.get_var_name_generator())

    kernel = AxisRemover(rule_mapping_context, var_name, axis_num).map_kernel(kernel)

    if len(kernel.temporary_variables[var_name].shape) == 1:
        new_temps = dict((tv.name, tv.copy(shape=(), dim_tags=None))
                if tv.name == var_name else (tv.name, tv) for tv in
                kernel.temporary_variables.values())
    else:
        from loopy import auto
        new_temps = dict((tv.name,
            tv.copy(shape=tv.shape[:axis_num]+tv.shape[axis_num+1:],
                strides=auto, dim_tags=None))
                if tv.name == var_name else (tv.name, tv) for tv in
                kernel.temporary_variables.values())

    return kernel.copy(temporary_variables=new_temps)


def remove_invariant_inames(kernel):
    inames_used = set()
    untagged_inames = (
            kernel.all_inames() - frozenset(kernel.iname_to_tags.keys()))
    for insn in kernel.instructions:
        for iname in ((insn.read_dependency_names()
            | insn.write_dependency_names())
        & untagged_inames):
            inames_used.add(iname)

    removable_inames = untagged_inames - inames_used

    new_insns = [insn.copy(within_inames=insn.within_inames-removable_inames)
            for insn in kernel.instructions]

    return remove_unused_inames(kernel.copy(instructions=new_insns),
            removable_inames)

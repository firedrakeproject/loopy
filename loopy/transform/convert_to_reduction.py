from loopy.diagnostic import LoopyError
from pymbolic.primitives import Sum


def convert_to_reduction(kernel, within, reduction_over_inames):
    insn_ids = []
    if isinstance(within, str):
        from loopy.match import parse_match
        within = parse_match(within)

    insn_ids = [insn.id for insn in kernel.instructions if within(kernel, insn)]
    if not insn_ids:
        raise LoopyError("No matching instructions found.")

    modified_insns = {}
    for insn_id in insn_ids:
        insn = kernel.id_to_insn[insn_id]
        assignee = insn.assignee
        rhs = insn.expression

        assert all(iname in insn.within_inames for iname in reduction_over_inames)

        # FIXME: Assuming that the expression is a Sum node

        assert isinstance(rhs, Sum)
        assert assignee in rhs.children

        redn_expr = rhs - assignee
        redn_expr = Sum(tuple(set(rhs.children)-set([assignee])))

        from loopy.symbolic import Reduction
        from loopy.library.reduction import SumReductionOperation
        redn = Reduction(SumReductionOperation(), inames=reduction_over_inames,
                expr=redn_expr, allow_simultaneous=len(insn_ids) > 1)

        modified_insns[insn_id] = insn.copy(expression=redn,
                within_inames=insn.within_inames-frozenset(reduction_over_inames))

    new_insns = [modified_insns[insn.id] if insn.id in insn_ids else insn for
            insn in kernel.instructions]

    return kernel.copy(instructions=new_insns)

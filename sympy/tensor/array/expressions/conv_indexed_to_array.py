from collections import defaultdict

from sympy import Sum, Mul, KroneckerDelta, Indexed, IndexedBase, Add, Pow, Integer, default_sort_key
from sympy.combinatorics import Permutation
from sympy.matrices.expressions.matexpr import MatrixElement
from sympy.tensor.array.expressions.array_expressions import PermuteDims, ArrayDiagonal, \
    ArrayContraction, ArrayTensorProduct, ArrayAdd, get_shape, ArrayElement
from sympy.tensor.array.expressions.utils import _get_argindex, _get_diagonal_indices


def convert_indexed_to_array(expr, first_indices=None):
    r"""
    Parse indexed expression into a form useful for code generation.

    Examples
    ========

    >>> from sympy.tensor.array.expressions.conv_indexed_to_array import convert_indexed_to_array
    >>> from sympy import MatrixSymbol, Sum, symbols

    >>> i, j, k, d = symbols("i j k d")
    >>> M = MatrixSymbol("M", d, d)
    >>> N = MatrixSymbol("N", d, d)

    Recognize the trace in summation form:

    >>> expr = Sum(M[i, i], (i, 0, d-1))
    >>> convert_indexed_to_array(expr)
    ArrayContraction(M, (0, 1))

    Recognize the extraction of the diagonal by using the same index `i` on
    both axes of the matrix:

    >>> expr = M[i, i]
    >>> convert_indexed_to_array(expr)
    ArrayDiagonal(M, (0, 1))

    This function can help perform the transformation expressed in two
    different mathematical notations as:

    `\sum_{j=0}^{N-1} A_{i,j} B_{j,k} \Longrightarrow \mathbf{A}\cdot \mathbf{B}`

    Recognize the matrix multiplication in summation form:

    >>> expr = Sum(M[i, j]*N[j, k], (j, 0, d-1))
    >>> convert_indexed_to_array(expr)
    ArrayContraction(ArrayTensorProduct(M, N), (1, 2))

    Specify that ``k`` has to be the starting index:

    >>> convert_indexed_to_array(expr, first_indices=[k])
    ArrayContraction(ArrayTensorProduct(N, M), (0, 3))
    """

    result, indices = _convert_indexed_to_array(expr)

    if any(isinstance(i, (int, Integer)) for i in indices):
        result = ArrayElement(result, indices)
        indices = []

    if not first_indices:
        return result

    def _check_is_in(elem, indices):
        if elem in indices:
            return True
        if any(elem in i for i in indices if isinstance(i, frozenset)):
            return True
        return False

    repl = {j: i for i in indices if isinstance(i, frozenset) for j in i}
    first_indices = [repl.get(i, i) for i in first_indices]
    for i in first_indices:
        if not _check_is_in(i, indices):
            first_indices.remove(i)
    first_indices.extend([i for i in indices if not _check_is_in(i, first_indices)])

    def _get_pos(elem, indices):
        if elem in indices:
            return indices.index(elem)
        for i, e in enumerate(indices):
            if not isinstance(e, frozenset):
                continue
            if elem in e:
                return i
        raise ValueError("not found")

    permutation = [_get_pos(i, first_indices) for i in indices]
    return PermuteDims(result, permutation)


def _convert_indexed_to_array(expr):
    if isinstance(expr, Sum):
        function = expr.function
        summation_indices = expr.variables
        subexpr, subindices = _convert_indexed_to_array(function)
        subindicessets = {j: i for i in subindices if isinstance(i, frozenset) for j in i}
        summation_indices = sorted(set([subindicessets.get(i, i) for i in summation_indices]), key=default_sort_key)
        # TODO: check that Kronecker delta is only contracted to one other element:
        kronecker_indices = set([])
        if isinstance(function, Mul):
            for arg in function.args:
                if not isinstance(arg, KroneckerDelta):
                    continue
                arg_indices = sorted(set(arg.indices), key=default_sort_key)
                if len(arg_indices) == 2:
                    kronecker_indices.update(arg_indices)
        kronecker_indices = sorted(kronecker_indices, key=default_sort_key)
        # Check dimensional consistency:
        shape = get_shape(subexpr)
        if shape:
            for ind, istart, iend in expr.limits:
                i = _get_argindex(subindices, ind)
                if istart != 0 or iend+1 != shape[i]:
                    raise ValueError("summation index and array dimension mismatch: %s" % ind)
        contraction_indices = []
        subindices = list(subindices)
        if isinstance(subexpr, ArrayDiagonal):
            diagonal_indices = list(subexpr.diagonal_indices)
            dindices = subindices[-len(diagonal_indices):]
            subindices = subindices[:-len(diagonal_indices)]
            for index in summation_indices:
                if index in dindices:
                    position = dindices.index(index)
                    contraction_indices.append(diagonal_indices[position])
                    diagonal_indices[position] = None
            diagonal_indices = [i for i in diagonal_indices if i is not None]
            for i, ind in enumerate(subindices):
                if ind in summation_indices:
                    pass
            if diagonal_indices:
                subexpr = ArrayDiagonal(subexpr.expr, *diagonal_indices)
            else:
                subexpr = subexpr.expr

        axes_contraction = defaultdict(list)
        for i, ind in enumerate(subindices):
            include = all(j not in kronecker_indices for j in ind) if isinstance(ind, frozenset) else ind not in kronecker_indices
            if ind in summation_indices and include:
                axes_contraction[ind].append(i)
                subindices[i] = None
        for k, v in axes_contraction.items():
            if any(i in kronecker_indices for i in k) if isinstance(k, frozenset) else k in kronecker_indices:
                continue
            contraction_indices.append(tuple(v))
        free_indices = [i for i in subindices if i is not None]
        indices_ret = list(free_indices)
        indices_ret.sort(key=lambda x: free_indices.index(x))
        return ArrayContraction(
                subexpr,
                *contraction_indices,
                free_indices=free_indices
            ), tuple(indices_ret)
    if isinstance(expr, Mul):
        args, indices = zip(*[_convert_indexed_to_array(arg) for arg in expr.args])
        # Check if there are KroneckerDelta objects:
        kronecker_delta_repl = {}
        for arg in args:
            if not isinstance(arg, KroneckerDelta):
                continue
            # Diagonalize two indices:
            i, j = arg.indices
            kindices = set(arg.indices)
            if i in kronecker_delta_repl:
                kindices.update(kronecker_delta_repl[i])
            if j in kronecker_delta_repl:
                kindices.update(kronecker_delta_repl[j])
            kindices = frozenset(kindices)
            for index in kindices:
                kronecker_delta_repl[index] = kindices
        # Remove KroneckerDelta objects, their relations should be handled by
        # ArrayDiagonal:
        newargs = []
        newindices = []
        for arg, loc_indices in zip(args, indices):
            if isinstance(arg, KroneckerDelta):
                continue
            newargs.append(arg)
            newindices.append(loc_indices)
        flattened_indices = [kronecker_delta_repl.get(j, j) for i in newindices for j in i]
        diagonal_indices, ret_indices = _get_diagonal_indices(flattened_indices)
        tp = ArrayTensorProduct(*newargs)
        if diagonal_indices:
            return (ArrayDiagonal(tp, *diagonal_indices), ret_indices)
        else:
            return tp, ret_indices
    if isinstance(expr, MatrixElement):
        indices = expr.args[1:]
        diagonal_indices, ret_indices = _get_diagonal_indices(indices)
        if diagonal_indices:
            return (ArrayDiagonal(expr.args[0], *diagonal_indices), ret_indices)
        else:
            return expr.args[0], ret_indices
    if isinstance(expr, Indexed):
        indices = expr.indices
        diagonal_indices, ret_indices = _get_diagonal_indices(indices)
        if diagonal_indices:
            return (ArrayDiagonal(expr.base, *diagonal_indices), ret_indices)
        else:
            return expr.args[0], ret_indices
    if isinstance(expr, IndexedBase):
        raise NotImplementedError
    if isinstance(expr, KroneckerDelta):
        return expr, expr.indices
    if isinstance(expr, Add):
        args, indices = zip(*[_convert_indexed_to_array(arg) for arg in expr.args])
        args = list(args)
        # Check if all indices are compatible. Otherwise expand the dimensions:
        index0set = set(indices[0])
        index0 = indices[0]
        for i in range(1, len(args)):
            if set(indices[i]) != index0set:
                raise NotImplementedError("indices must be the same")
            permutation = Permutation([index0.index(j) for j in indices[i]])
            # Perform index permutations:
            args[i] = PermuteDims(args[i], permutation)
        return ArrayAdd(*args), index0
    if isinstance(expr, Pow):
        subexpr, subindices = _convert_indexed_to_array(expr.base)
        if isinstance(expr.exp, (int, Integer)):
            diags = zip(*[(2*i, 2*i + 1) for i in range(expr.exp)])
            arr = ArrayDiagonal(ArrayTensorProduct(*[subexpr for i in range(expr.exp)]), *diags)
            return arr, subindices
    return expr, ()

import os
import numpy as np
from numpy.typing import NDArray
from typing import Any, Dict, Tuple, Optional
from fibsem_tools.io.zarr import lock_array
from fibsem_tools.metadata.cosem import COSEMGroupMetadata, SpatialTransform
from fibsem_tools.metadata.neuroglancer import NeuroglancerN5GroupMetadata
from xarray import DataArray
from xarray_multiscale.reducers import windowed_mode
from fibsem_tools.io import initialize_group
from fibsem_tools.io.dask import store_blocks


from itertools import combinations
from functools import reduce

import math


class Multiscales:
    def __init__(self,
                 name: str,
                 arrays: Dict[str, DataArray],
                 attrs: Dict[str, Any] = {}
                 ):
        """
        Create a representation of a multiresolution collection of arrays.
        This class is basically a string name, a dict-of-arrays representing a
        multiresolution pyramid, and a dict of attributes associated with
        the multiresolution pyramid.

        Parameters
        ----------

        arrays : dict of xarray.DataArray
            The keys of this dict will be used as the names of the individual arrays when serialized to storage.

        attrs : dict
            Attributes associated

        Returns an instance of `Multiscales`
        -------
        """
        if not isinstance(arrays, dict):
            raise ValueError("`arrays` must be a dict of xarray.DataArray")
        else:
            if not all(isinstance(x, DataArray) for x in arrays.values()):
                raise ValueError("`arrays` must be a dict of xarray.DataArray")
        self.arrays: Dict[str, DataArray] = arrays
        self.attrs = attrs
        self.name = name

    def __repr__(self):
        return str(self.arrays)

    def _group_metadata(self):
        cosem_meta = COSEMGroupMetadata.fromDataArrays(
            name=self.name,
            dataarrays=tuple(self.arrays.values()),
            paths=tuple(self.arrays.keys()),
        ).dict()

        neuroglancer_meta = NeuroglancerN5GroupMetadata.fromDataArrays(
            tuple(self.arrays.values())
        ).dict()

        return {**cosem_meta, **neuroglancer_meta}

    def _array_metadata(self):
        cosem_meta = {
            key: {"transform": SpatialTransform.fromDataArray(arr).dict()}
            for key, arr in self.arrays.items()
        }
        return cosem_meta

    def store(
        self,
        uri: str,
        chunks: Optional[Tuple[int, ...]] = None,
        multiscale_metadata: bool = True,
        propagate_array_attrs: bool = True,
        locking: bool=False,
        client=None,
        access_modes = ('a', 'a'),
        **kwargs
    ):
        """
        Prepare to store the multiscale arrays.

        Parameters
        ----------

        uri : str
            Path to the storage location.

        chunks : tuple of ints, or dict of tuples of ints
            The chunking used for the arrays in storage. If a single tuple of ints is provided,
            all output arrays will be created with a uniform same chunking scheme. If a dict of tuples
            of ints is provided, then the chunking scheme for an array with key K will be specified by
            output_chunks[K]

        multiscale_metadata : bool, default=True
            Whether to add multiscale-specific metadata the zarr / n5 group and arrays created in storage. If True,
            both cosem/ome-style metadata (for the group and the arrays) and neuroglancer-style metadata will be created.

        propagate_array_attrs : bool, default=True
            Whether to propagate the values in the .attrs property of each array to the attributes
            of the serialized arrays. Note that the process of copying array attrs before after the creation
            of multiscale metadata (governed by the `multiscale_metadata` keyword argument), so any
            name collisions will be resolved in favor of the metadata generated by the multiscale_metadata step.

        locks : lock: Lock-like, str, or False
            Locks to use before writing. Requires a distributed client.

        client : distributed.Client or None. default=None
            A distributed.client object to register locks with.

        Returns
        -------
        store_group, store_arrays, storage_ops
            A length-3 tuple containing a reference to the newly created group, the newly created arrays, and a
            list of list of dask.delayed objects, each of which when computed will generate a region of the multiscale
            pyramid and save the results to disk.

        """

        group_attrs = self.attrs.copy()
        array_attrs: Dict[str, Any] = {k: {} for k in self.arrays}

        if propagate_array_attrs:
            array_attrs = {k: dict(v.attrs) for k, v in self.arrays.items()}

        if multiscale_metadata:
            group_attrs.update(self._group_metadata())
            _array_meta = self._array_metadata()
            for k in self.arrays:
                array_attrs[k].update(_array_meta[k])

        if chunks is None:
            _chunks = {key: v.data.chunksize for key, v in self.arrays.items()}
        else:
            _chunks = {key: chunks for key in self.arrays}
        store_group = initialize_group(
            uri,
            self.arrays.values(),
            array_paths=self.arrays.keys(),
            chunks=_chunks.values(),
            group_attrs=group_attrs,
            array_attrs=array_attrs.values(),
            modes = access_modes,
            **kwargs
        )
        store_arrays = [store_group[key] for key in self.arrays.keys()]
        # create locks for the arrays with misaligned chunks
        if locking:
            if client is None:
                raise ValueError(
                    "Supply an instance of distributed.Client to use locking."
                )
            locked_arrays = []
            for store_array in store_arrays:
                if np.any(
                    np.mod(
                        self.arrays[store_array.basename].data.chunksize,
                        store_array.chunks,
                    )
                    > 0
                ):
                    locked_arrays.append(lock_array(store_array, client))
                else:
                    locked_arrays.append(store_array)
            store_arrays = locked_arrays

        storage_ops = store_blocks([v.data for v in self.arrays.values()],
                                   store_arrays)
        return store_group, store_arrays, storage_ops


def mode_reduce(array: NDArray[Any],
                window_size: Tuple[int, ...]) -> NDArray[Any]:
    if np.all(np.array(window_size) == 2):
        result = countless(array, window_size)
    else:
        result = windowed_mode(array, window_size)

    return result


def countless(data, factor) -> NDArray[Any]:
    """
    countless downsamples labeled images (segmentations)
    by finding the mode using vectorized instructions.
    It is ill advised to use this O(2^N-1) time algorithm
    and O(NCN/2) space for N > about 16 tops.
    This means it's useful for the following kinds
    of downsampling.
    This could be implemented for higher performance in
    C/Cython more simply, but at least this is easily
    portable.
    2x2x1 (N=4), 2x2x2 (N=8), 4x4x1 (N=16), 3x2x1 (N=6)
    and various other configurations of a similar nature.
    c.f. https://medium.com/@willsilversmith/countless-3d-vectorized-2x-downsampling-of-labeled-volume-images-using-python-and-numpy-59d686c2f75

    This function has been modified from the original
    to avoid mutation of the input argument.
    """
    sections = []

    mode_of = reduce(lambda x, y: x * y, factor)
    majority = int(math.ceil(float(mode_of) / 2))

    for offset in np.ndindex(factor):
        part = data[tuple(np.s_[o::f] for o, f in zip(offset, factor))] + 1
        sections.append(part)

    pick = lambda a, b: a * (a == b)
    lor = lambda x, y: x + (x == 0) * y  # logical or

    subproblems = [{}, {}]
    results2 = None
    for x, y in combinations(range(len(sections) - 1), 2):
        res = pick(sections[x], sections[y])
        subproblems[0][(x, y)] = res
        if results2 is not None:
            results2 = lor(results2, res)
        else:
            results2 = res

    results = [results2]
    for r in range(3, majority + 1):
        r_results = None
        for combo in combinations(range(len(sections)), r):
            res = pick(subproblems[0][combo[:-1]], sections[combo[-1]])

            if combo[-1] != len(sections) - 1:
                subproblems[1][combo] = res

            if r_results is not None:
                r_results = lor(r_results, res)
            else:
                r_results = res
        results.append(r_results)
        subproblems[0] = subproblems[1]
        subproblems[1] = {}

    results.reverse()
    final_result = lor(reduce(lor, results), sections[-1]) - 1

    return final_result

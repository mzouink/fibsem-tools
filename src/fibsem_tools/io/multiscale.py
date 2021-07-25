from typing import Any, Dict, Tuple, Optional
from distributed import Lock
import numpy as np
from fibsem_tools.io.zarr import lock_array
from fibsem_tools.metadata.cosem import COSEMGroupMetadata, SpatialTransform, ScaleMeta
from fibsem_tools.metadata.neuroglancer import NeuroglancerN5GroupMetadata
from xarray import DataArray

from fibsem_tools.io import initialize_group
from fibsem_tools.io.dask import store_blocks


class Multiscales:
    def __init__(
        self, name: str, arrays: Dict[str, DataArray], attrs: Dict[str, Any] = {}
    ):
        """
        Create a representation of a multiresolution collection of arrays.
        This class is basically a string name, a dict-of-arrays representing a multiresolution pyramid,
        and a dict of attributes associated with the multiresolution pyramid.

        Parameters
        ----------

        name : str,
            The name associated with this multiscale collection. When storing the collection,
            `name` is used as the name of the group or folder in storage that contains the collection
            of arrays.

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
        store: str,
        chunks: Optional[Tuple[int]] = None,
        multiscale_metadata: bool = True,
        propagate_array_attrs: bool = True,
        locking=False,
        client=None,
        **kwargs
    ):
        """
        Prepare to store the multiscale arrays.

        Parameters
        ----------

        store : str
            Path to the root storage location.
            When saving to zarr or n5 (the only two modes currently supported),
            `store` should be the root of the zarr / n5 hierarchy, e.g. `store='foo/bar.n5'`

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
        elif isinstance(chunks, (tuple, list)):
            _chunks = {key: chunks for key in self.arrays}

        store_group, store_arrays = initialize_group(
            store,
            self.name,
            tuple(self.arrays.values()),
            array_paths=tuple(self.arrays.keys()),
            chunks=tuple(_chunks.values()),
            group_attrs=group_attrs,
            array_attrs=tuple(array_attrs.values()),
            **kwargs
        )
        # create locks for the arrays with misaligned chunks
        if locking:
            if client is None:
                raise ValueError('Supply an instance of distributed.Client to use locking.')
            locked_arrays = []
            for store_array in store_arrays:
                if np.any(np.mod(self.arrays[store_array.path].data.chunksize, store_array.chunks) > 0):
                    locked_arrays.append(lock_array(store_array, client))
                else:
                    locked_arrays.append(store_array)
            store_arrays = locked_arrays
        
        storage_ops = store_blocks([v.data for v in self.arrays.values()], store_arrays)
        return store_group, store_arrays, storage_ops

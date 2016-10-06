"""
Create statistical summaries command

"""

from __future__ import absolute_import, print_function

import logging
from collections import OrderedDict
from functools import reduce as reduce_, partial
from pathlib import Path

import click
import numpy
import pandas as pd
import rasterio
import xarray

from datacube import Datacube
from datacube.api import make_mask
from datacube.api.grid_workflow import GridWorkflow, Tile
from datacube.api.query import query_group_by, query_geopolygon
from datacube.model import GridSpec, CRS, Coordinate, Variable, DatasetType, GeoBox
from datacube.model.utils import make_dataset, datasets_to_doc, xr_apply
from datacube.storage import netcdf_writer
from datacube.storage.masking import mask_valid_data as mask_invalid_data
from datacube.storage.storage import create_netcdf_storage_unit
from datacube.ui import click as ui
from datacube.ui.click import to_pathlib
from datacube.utils import read_documents, unsqueeze_data_array, import_function, tile_iter
from datacube.utils.dates import date_sequence
from datacube_apps.stats.statistics import argnanmedoid, argpercentile, axisindex

_LOG = logging.getLogger(__name__)

STANDARD_VARIABLE_PARAM_NAMES = {'zlib',
                                 'complevel',
                                 'shuffle',
                                 'fletcher32',
                                 'contiguous',
                                 'attrs'}
DEFAULT_GROUP_BY = 'time'


def datetime64_to_inttime(var):
    """
    Return an "inttime" representing a datetime64.

    For example, 2016-09-29 as an "inttime" would be 20160929

    :param var: datetime64
    :return: int representing the given time
    """
    values = getattr(var, 'values', var)
    years = values.astype('datetime64[Y]').astype('int32') + 1970
    months = values.astype('datetime64[M]').astype('int32') % 12 + 1
    days = (values.astype('datetime64[D]') - values.astype('datetime64[M]') + 1).astype('int32')
    return years * 10000 + months * 100 + days


class ValueStat(object):
    """
    Holder class describing the outputs of a statistic and how to calculate it

    :param stat_func: callable to compute statistics
    :param bool masked: whether to apply masking to the input data
    """

    def __init__(self, stat_func, masked=True):
        self.masked = masked
        self.stat_func = stat_func

    def compute(self, data):
        """
        Compute a statistic on the given Dataset.

        :param xarray.Dataset data:
        :return: xarray.Dataset
        """
        return self.stat_func(data)

    @staticmethod
    def measurements(input_measurements):
        """
        Turn a list of input measurements into a list of output measurements.

        :param input_measurements:
        :rtype: list(dict)
        """
        return [
            {attr: measurement[attr] for attr in ['name', 'dtype', 'nodata', 'units']}
            for measurement in input_measurements]

    @classmethod
    def from_stat_name(cls, name, masked=True):
        """
        A value returning statistic, relying on an xarray function of `name` being available

        :param name: The name of an `xarray.Dataset` statistical function
        :param masked:
        :return:
        """
        return cls(masked=masked,
                   stat_func=partial(getattr(xarray.Dataset, name), dim='time'))


class WofsStats(object):
    def __init__(self):
        self.masked = True

    @staticmethod
    def compute(data):
        wet = (data.water == 128).sum(dim='time')
        dry = (data.water == 0).sum(dim='time')
        clear = wet + dry
        frequency = wet / clear
        return xarray.Dataset({'count_wet': wet,
                               'count_clear': clear,
                               'frequency': frequency}, attrs=dict(crs=data.crs))

    @staticmethod
    def measurements(input_measurements):
        measurement_names = set(m['name'] for m in input_measurements)
        assert 'water' in measurement_names
        return [
            {
                'name': 'count_wet',
                'dtype': 'int16',
                'nodata': -1,
                'units': '1'
            },
            {
                'name': 'count_clear',
                'dtype': 'int16',
                'nodata': -1,
                'units': '1'
            },
            {
                'name': 'frequency',
                'dtype': 'float32',
                'nodata': -1,
                'units': '1'
            },

        ]


class NormalisedDifferenceStats(object):
    """
    Simple NDVI/NDWI and other Normalised Difference stats

    Computes (band1 - band2)/(band1 + band2), and then summarises using the list of `stats` into
    separate output variables.
    """

    def __init__(self, band1, band2, name, stats=None, masked=True):
        self.stats = stats if stats else ['min', 'max', 'mean']
        self.band1 = band1
        self.band2 = band2
        self.name = name
        self.masked = masked

    def compute(self, data):
        nd = (data[self.band1] - data[self.band2]) / (data[self.band1] + data[self.band2])
        outputs = {}
        for stat in self.stats:
            name = '_'.join([self.name, stat])
            outputs[name] = getattr(nd, stat)(dim='time')
        return xarray.Dataset(outputs,
                              attrs=dict(crs=data.crs))

    def measurements(self, input_measurements):
        measurement_names = [m['name'] for m in input_measurements]
        if self.band1 not in measurement_names or self.band2 not in measurement_names:
            raise ConfigurationError('Input measurements for %s must include "%s" and "%s"',
                                     self.name, self.band1, self.band2)

        return [dict(name='_'.join([self.name, stat]), dtype='float32', nodata=-1, units='1')
                for stat in self.stats]


class PerBandIndexStat(ValueStat):
    """
    Each output variable contains values that actually exist in the input data

    :param stat_func: A function which takes an xarray.Dataset and returns an xarray.Dataset of indexes
    """

    def __init__(self, stat_func, masked=True):
        super(PerBandIndexStat, self).__init__(stat_func, masked)

    def compute(self, data):
        index = super(PerBandIndexStat, self).compute(data)

        def index_dataset(var):
            return axisindex(data.data_vars[var.name].values, var.values)

        data_values = index.apply(index_dataset)

        def index_time(var):
            return data.time.values[var.values]

        time_values = index.apply(index_time).rename(OrderedDict((name, name + '_observed')
                                                                 for name in index.data_vars))

        text_values = time_values.apply(datetime64_to_inttime).rename(OrderedDict((name, name + '_date')
                                                                                  for name in time_values.data_vars))

        def index_source(var):
            return data.source.values[var.values]

        time_values = index.apply(index_source).rename(OrderedDict((name, name + '_source')
                                                                   for name in index.data_vars))

        return xarray.merge([data_values, time_values, text_values])

    @staticmethod
    def measurements(input_measurements):
        index_measurements = [
            {
                'name': measurement['name'] + '_source',
                'dtype': 'int8',
                'nodata': -1,
                'units': '1'
            }
            for measurement in input_measurements
            ]
        date_measurements = [
            {
                'name': measurement['name'] + '_observed',
                'dtype': 'float64',
                'nodata': 0,
                'units': 'seconds since 1970-01-01 00:00:00'
            }
            for measurement in input_measurements
            ]
        text_measurements = [
            {
                'name': measurement['name'] + '_observed_date',
                'dtype': 'int32',
                'nodata': 0,
                'units': 'Date as YYYYMMDD'
            }
            for measurement in input_measurements
            ]

        return ValueStat.measurements(input_measurements) + date_measurements + index_measurements + text_measurements


class PerStatIndexStat(ValueStat):
    """
    :param stat_func: A function which takes an xarray.Dataset and returns an xarray.Dataset of indexes
    """

    def __init__(self, stat_func, masked=True):
        super(PerStatIndexStat, self).__init__(stat_func, masked)

    def compute(self, data):
        index = super(PerStatIndexStat, self).compute(data)

        def index_dataset(var, axis):
            return axisindex(var, index, axis=axis)

        data_values = data.reduce(index_dataset, dim='time')
        observed = data.time.values[index]
        data_values['observed'] = (('y', 'x'), observed)
        data_values['observed_date'] = (('y', 'x'), datetime64_to_inttime(observed))
        data_values['source'] = (('y', 'x'), data.source.values[index])

        return data_values

    @staticmethod
    def measurements(input_measurements):
        index_measurements = [
            {
                'name': 'source',
                'dtype': 'int8',
                'nodata': -1,
                'units': '1'
            }
        ]
        date_measurements = [
            {
                'name': 'observed',
                'dtype': 'float64',
                'nodata': 0,
                'units': 'seconds since 1970-01-01 00:00:00'
            }
        ]
        text_measurements = [
            {
                'name': 'observed_date',
                'dtype': 'int32',
                'nodata': 0,
                'units': 'Date as YYYYMMDD'
            }
        ]
        return ValueStat.measurements(input_measurements) + date_measurements + index_measurements + text_measurements


def _medoid_helper(data):
    flattened = data.to_array(dim='variable')
    variable, time, y, x = flattened.shape
    index = numpy.empty((y, x), dtype='int64')
    # TODO: nditer?
    for iy in range(y):
        for ix in range(x):
            index[iy, ix] = argnanmedoid(flattened.values[:, :, iy, ix])
    return index


def _make_percentile_stat(q):
    return PerBandIndexStat(masked=True,
                            # pylint: disable=redundant-keyword-arg
                            stat_func=partial(getattr(xarray.Dataset, 'reduce'),
                                              dim='time',
                                              func=argpercentile,
                                              q=q))


STATS = {
    'min': ValueStat.from_stat_name('min'),
    'max': ValueStat.from_stat_name('max'),
    'mean': ValueStat.from_stat_name('mean'),
    'percentile_10': _make_percentile_stat(10),
    'percentile_50': _make_percentile_stat(50),
    'percentile_90': _make_percentile_stat(90),
    'medoid': PerStatIndexStat(masked=True, stat_func=_medoid_helper),
    'ndvi_stats': NormalisedDifferenceStats(name='ndvi', band1='nir', band2='red',
                                            stats=['min', 'mean', 'max']),
    'ndwi_stats': NormalisedDifferenceStats(name='ndwi', band1='green', band2='swir1',
                                            stats=['min', 'mean', 'max']),
    'wofs': WofsStats(),

}


class StatProduct(object):
    def __init__(self, metadata_type, input_measurements, definition, storage):
        self.definition = definition

        data_measurements = self.statistic.measurements(input_measurements)

        self.product = self._create_product(metadata_type, data_measurements, storage)
        self.netcdf_var_params = self._create_netcdf_var_params(storage, data_measurements)

    @property
    def name(self):
        return self.definition['name']

    @property
    def stat_name(self):
        return self.definition['statistic']

    @property
    def statistic(self):
        return STATS[self.stat_name]

    @property
    def masked(self):
        return self.statistic.masked

    @property
    def compute(self):
        return self.statistic.compute

    def _create_netcdf_var_params(self, storage, data_measurements):
        chunking = storage['chunking']
        chunking = [chunking[dim] for dim in storage['dimension_order']]

        variable_params = {}
        for measurement in data_measurements:
            name = measurement['name']
            variable_params[name] = {k: v for k, v in self.definition.items() if k in STANDARD_VARIABLE_PARAM_NAMES}
            variable_params[name]['chunksizes'] = chunking
            variable_params[name].update({k: v for k, v in measurement.items() if k in STANDARD_VARIABLE_PARAM_NAMES})
        return variable_params

    def _create_product(self, metadata_type, data_measurements, storage):
        product_definition = {
            'name': self.name,
            'description': 'Description for ' + self.name,
            'metadata_type': 'eo',
            'metadata': {
                'format': 'NetCDF',
                'product_type': self.stat_name,
            },
            'storage': storage,
            'measurements': data_measurements
        }
        DatasetType.validate(product_definition)
        return DatasetType(metadata_type, product_definition)


class StatsConfig(object):
    """
    A StatsConfig contains everything required to describe a production of a set of time based statistics products.

    Includes:
    - storage: Description of output file format
    - sources: List of input products, variables and masks
    - output_products: List of filenames and statistical methods used, describing what the outputs of the run will be.
    - start_time, end_time, stats_duration, step_size: How to group across time.
    - computation: How to split the job up to fit into memory.
    - location: top level directory to save files
    - input_region: optionally restrict the processing spatial area
    """
    def __init__(self, config):
        self._definition = config

        self.storage = config['storage']

        self.sources = config['sources']
        self.output_products = config['output_products']

        self.start_time = pd.to_datetime(config['start_date'])
        self.end_time = pd.to_datetime(config['end_date'])
        self.stats_duration = config['stats_duration']
        self.step_size = config['step_size']

        self.grid_spec = self.make_grid_spec()
        self.location = config['location']
        self.computation = config['computation']
        self.input_region = config.get('input_region')

    def make_grid_spec(self):
        """Make a grid spec based on `self.storage`."""
        if 'tile_size' not in self.storage:
            return None

        crs = CRS(self.storage['crs'])
        return GridSpec(crs=crs,
                        tile_size=[self.storage['tile_size'][dim] for dim in crs.dimensions],
                        resolution=[self.storage['resolution'][dim] for dim in crs.dimensions])


def get_filename(path_template, **kwargs):
    return Path(str(path_template).format(**kwargs))


def find_source_datasets(task, stat, geobox, uri=None):
    def _make_dataset(labels, sources):
        dataset = make_dataset(product=stat.product,
                               sources=sources,
                               extent=geobox.extent,
                               center_time=labels['time'],
                               uri=uri,
                               app_info=None,  # TODO: Add stats application information
                               valid_data=None)  # TODO: Add valid region geopolygon
        return dataset

    def merge_sources(prod):
        if stat.masked:
            all_sources = xarray.align(prod['data'].sources, *[mask_tile.sources for mask_tile in prod['masks']])
            return reduce_(lambda a, b: a + b, (sources.sum() for sources in all_sources))
        else:
            return prod['data'].sources.sum()

    start_time, _ = task.time_period
    sources = reduce_(lambda a, b: a + b, (merge_sources(prod) for prod in task.sources))
    sources = unsqueeze_data_array(sources, dim='time', pos=0, coord=start_time,
                                   attrs=task.time_attributes)

    datasets = xr_apply(sources, _make_dataset, dtype='O')  # Store in DataArray to associate Time -> Dataset
    datasets = datasets_to_doc(datasets)
    return datasets, sources


def load_masked_data(sub_tile_slice, source_prod):
    data = GridWorkflow.load(source_prod['data'][sub_tile_slice],
                             measurements=source_prod['spec']['measurements'])
    crs = data.crs
    data = mask_invalid_data(data)

    if 'masks' in source_prod and 'masks' in source_prod['spec']:
        for mask_spec, mask_tile in zip(source_prod['spec']['masks'], source_prod['masks']):
            fuse_func = import_function(mask_spec['fuse_func']) if 'fuse_func' in mask_spec else None
            mask = GridWorkflow.load(mask_tile[sub_tile_slice],
                                     measurements=[mask_spec['measurement']],
                                     fuse_func=fuse_func)[mask_spec['measurement']]
            mask = make_mask(mask, **mask_spec['flags'])
            data = data.where(mask)
            del mask
    data.attrs['crs'] = crs
    return data


def load_data(sub_tile_slice, sources):
    datasets = [load_masked_data(sub_tile_slice, source_prod) for source_prod in sources]
    for idx, dataset in enumerate(datasets):
        dataset.coords['source'] = ('time', numpy.repeat(idx, dataset.time.size))
    data = xarray.concat(datasets, dim='time')
    return data.isel(time=data.time.argsort())  # sort along time dim


class OutputDriver(object):
    # TODO: Add check for valid filename extensions in each driver
    def __init__(self, task, config):
        self.task = task
        self.config = config

        self.output_files = {}

    def close_files(self):
        for output_file in self.output_files.values():
            output_file.close()

    def open_output_files(self):
        raise NotImplementedError

    def write_data(self, prod_name, var_name, tile_index, values):
        raise NotImplementedError

    def _get_dtype(self, prod_name, var_name):
        return self.task.output_products[prod_name].product.measurements[var_name]['dtype']

    def __enter__(self):
        self.open_output_files()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close_files()


class NetcdfOutputDriver(OutputDriver):
    """
    Write data to Datacube compatible NetCDF files
    """

    def open_output_files(self):
        for prod_name, stat in self.task.output_products.items():
            filename_template = str(Path(self.config.location, stat.definition['file_path_template']))
            self.output_files[prod_name] = self._create_storage_unit(stat, filename_template)

    def _create_storage_unit(self, stat, filename_template):
        geobox = self.task.geobox
        all_measurement_defns = list(stat.product.measurements.values())

        output_filename = get_filename(filename_template, **self.task)
        datasets, sources = find_source_datasets(self.task, stat, geobox, uri=output_filename.as_uri())

        nco = self.nco_from_sources(sources,
                                    geobox,
                                    all_measurement_defns,
                                    stat.netcdf_var_params,
                                    output_filename)

        netcdf_writer.create_variable(nco, 'dataset', datasets, zlib=True)
        nco['dataset'][:] = netcdf_writer.netcdfy_data(datasets.values)
        return nco

    @staticmethod
    def nco_from_sources(sources, geobox, measurements, variable_params, filename):
        coordinates = OrderedDict((name, Coordinate(coord.values, coord.units))
                                  for name, coord in sources.coords.items())
        coordinates.update(geobox.coordinates)

        variables = OrderedDict((variable['name'], Variable(dtype=numpy.dtype(variable['dtype']),
                                                            nodata=variable['nodata'],
                                                            dims=sources.dims + geobox.dimensions,
                                                            units=variable['units']))
                                for variable in measurements)

        return create_netcdf_storage_unit(filename, geobox.crs, coordinates, variables, variable_params)

    def write_data(self, prod_name, var_name, tile_index, values):
        self.output_files[prod_name][var_name][(0,) + tile_index[1:]] = netcdf_writer.netcdfy_data(values)
        self.output_files[prod_name].sync()
        _LOG.debug("Updated %s %s", var_name, tile_index[1:])


class RioOutputDriver(OutputDriver):
    """
    Save data to file/s using rasterio. Eg. GeoTiff
    """

    def open_output_files(self):
        for prod_name, stat in self.task.output_products.items():
            for measurename, measure_def in stat.product.measurements.items():
                filename_template = str(Path(self.config.location, stat.definition['file_path_template']))
                geobox = self.task.geobox

                output_filename = get_filename(filename_template,
                                               var_name=measurename,
                                               config=self.config,
                                               **self.task)
                try:
                    output_filename.parent.mkdir(parents=True)
                except OSError:
                    pass

                profile = {
                    'blockxsize': self.config.storage['chunking']['x'],
                    'blockysize': self.config.storage['chunking']['y'],
                    'compress': 'lzw',
                    'driver': 'GTiff',
                    'interleave': 'band',
                    'tiled': True,
                    'dtype': measure_def['dtype'],
                    'nodata': measure_def['nodata'],
                    'width': geobox.width,
                    'height': geobox.height,
                    'affine': geobox.affine,
                    'crs': geobox.crs.crs_str,
                    'count': 1
                }

                output_name = prod_name + measurename
                self.output_files[output_name] = rasterio.open(str(output_filename), mode='w', **profile)

    def write_data(self, prod_name, var_name, tile_index, values):
        output_name = prod_name + var_name
        y, x = tile_index[1:]
        window = ((y.start, y.stop), (x.start, x.stop))
        _LOG.debug("Updating %s.%s %s", prod_name, var_name, window)

        dtype = self._get_dtype(prod_name, var_name)

        self.output_files[output_name].write(values.astype(dtype), indexes=1, window=window)


OUTPUT_DRIVERS = {
    'NetCDF CF': NetcdfOutputDriver,
    'Geotiff': RioOutputDriver
}


def do_stats(task, config):
    output_driver = OUTPUT_DRIVERS[config.storage['driver']]
    with output_driver(task, config) as output_files:
        example_tile = task.sources[0]['data']
        for sub_tile_slice in tile_iter(example_tile, config.computation['chunking']):
            data = load_data(sub_tile_slice, task.sources)

            for prod_name, stat in task.output_products.items():
                _LOG.info("Computing %s in tile %s", prod_name, sub_tile_slice)
                assert stat.masked  # TODO: not masked
                stats_data = stat.compute(data)

                # For each of the data variables, shove this chunk into the output results
                for var_name, var in stats_data.data_vars.items():
                    output_files.write_data(prod_name, var_name, sub_tile_slice, var.values)


class StatsTask(object):
    def __init__(self, time_period, tile_index=None, sources=None, output_products=None):
        self.tile_index = tile_index
        self.time_period = time_period
        self.sources = sources or []
        self.output_products = output_products or []

    @property
    def geobox(self):
        return self.sources[0]['data'].geobox  # TODO: Find a better way

    @property
    def time_attributes(self):
        return self.sources[0]['data'].sources.time.attrs

    def keys(self):
        return self.__dict__.keys()

    def __getitem__(self, item):
        return getattr(self, item)


def make_tasks(index, config, output_products):
    """
    Generate a sequence of `StatsTask` definitions.

    A Task Definition contains:

      * tile_index
      * time_period
      * sources: (list of)
      * output_products

    Sources is a list of dictionaries containing:

      * data
      * masks (list of)
      * spec - Source specification, containing details about which bands to load and how to apply masks.

    :param output_products: List of output product definitions
    :return:
    """
    if config.input_region:
        task_generator = make_tasks_non_grid
    else:
        task_generator = make_tasks_grid

    for task in task_generator(index, config):
        task.output_products = output_products
        yield task


def make_tasks_grid(index, config):
    """
    Generate the required tasks through time and across a spatial grid.

    :param index: Datacube Index
    :param config: StatsConfig
    :return:
    """
    workflow = GridWorkflow(index, grid_spec=config.grid_spec)
    for time_period in date_sequence(start=config.start_time, end=config.end_time,
                                     stats_duration=config.stats_duration, step_size=config.step_size):
        _LOG.info('Making output_products tasks for time period: %s', time_period)

        # Tasks are grouped by tile_index, and may contain sources from multiple places
        # Each source may be masked by multiple masks
        tasks = {}
        for source_spec in config.sources:
            data = workflow.list_cells(product=source_spec['product'], time=time_period,
                                       group_by=source_spec.get('group_by', DEFAULT_GROUP_BY))
            masks = [workflow.list_cells(product=mask['product'],
                                         time=time_period,
                                         group_by=source_spec.get('group_by', DEFAULT_GROUP_BY))
                     for mask in source_spec.get('masks', [])]

            for tile_index, sources in data.items():
                task = tasks.setdefault(tile_index, StatsTask(tile_index, time_period))
                task.sources.append({
                    'data': sources,
                    'masks': [mask.get(tile_index) for mask in masks],
                    'spec': source_spec,
                })

        for task in tasks.values():
            yield task


def make_tasks_non_grid(index, config):
    """
    Make stats tasks for a defined spatial region, that doesn't fit into a standard grid.

    Looks for an `input_region` section in the configuration file, with defined x/y spatial boundaries.

    :param index: database index
    :param config: StatsConfig
    :return:
    """
    dc = Datacube(index=index)

    def make_tile(product, time, group_by_name):
        datasets = dc.product_observations(product=product, time=time, **config.input_region)
        group_by = query_group_by(group_by=group_by_name)
        sources = dc.product_sources(datasets, group_by)

        res = config.storage['resolution']

        geopoly = query_geopolygon(**config.input_region)
        geopoly = geopoly.to_crs(CRS(config.storage['crs']))
        geobox = GeoBox.from_geopolygon(geopoly, (res['y'], res['x']))

        return Tile(sources, geobox)

    for time_period in date_sequence(start=config.start_time, end=config.end_time,
                                     stats_duration=config.stats_duration, step_size=config.step_size):
        _LOG.info('Making output_products tasks for time period: %s', time_period)

        task = StatsTask(time_period)

        for source_spec in config.sources:
            group_by_name = source_spec.get('group_by', DEFAULT_GROUP_BY)

            # Build Tile
            data = make_tile(product=source_spec['product'], time=time_period,
                             group_by_name=group_by_name)

            masks = [make_tile(product=mask['product'], time=time_period,
                               group_by_name=group_by_name)
                     for mask in source_spec.get('masks', [])]

            if len(data.sources.time) == 0:
                continue

            task.sources.append({
                'data': data,
                'masks': masks,
                'spec': source_spec,
            })

        yield task


class ConfigurationError(RuntimeError):
    pass


def make_products(index, config):
    _LOG.info('Creating output products')

    output_names = [prod['name'] for prod in config.output_products]
    duplicate_names = [x for x in output_names if output_names.count(x) > 1]
    if duplicate_names:
        raise ConfigurationError('Output products must all have different names. '
                                 'Duplicates found: %s' % duplicate_names)
    # TODO: Add more sanity checking. Eg. check desired 'statistic' is available

    output_products = {}

    measurements = source_product_measurement_defns(index, config.sources)

    for prod in config.output_products:
        output_products[prod['name']] = StatProduct(index.metadata_types.get_by_name('eo'), measurements,
                                                    definition=prod, storage=config.storage)

    return output_products


def source_product_measurement_defns(index, sources):
    """
    Look up desired measurements from sources in the database index

    :return: list of measurement definitions
    """
    # Check consistent measurements
    first_source = sources[0]
    if not all(first_source['measurements'] == source['measurements'] for source in sources):
        raise RuntimeError("Configuration Error: listed measurements of source products are not all the same.")

    source_defn = sources[0]

    source_product = index.products.get_by_name(source_defn['product'])
    measurements = [measurement for name, measurement in source_product.measurements.items()
                    if name in source_defn['measurements']]

    return measurements


def get_app_metadata(config, config_file):
    return {
        'lineage': {
            'algorithm': {
                'name': 'datacube-stats',
                'version': config.get('version', 'unknown'),
                'repo_url': 'https://github.com/GeoscienceAustralia/agdc_statistics.git',
                'parameters': {'configuration_file': config_file}
            },
        }
    }


@click.command(name='output_products')
@click.option('--app-config', '-c',
              type=click.Path(exists=True, readable=True, writable=False, dir_okay=False),
              help='configuration file location', callback=to_pathlib)
@click.option('--year', type=click.IntRange(1960, 2060))
@ui.global_cli_options
@ui.executor_cli_options
@ui.pass_index(app_name='agdc-output_products')
def main(index, app_config, year, executor):
    _, config = next(read_documents(app_config))

    config = StatsConfig(config)

    output_products = make_products(index, config)
    tasks = make_tasks(index, config, output_products)

    futures = [executor.submit(do_stats, task, config) for task in tasks]

    for future in executor.as_completed(futures):
        result = executor.result(future)
        print('Completed: %s' % result)
        # TODO: Record new datasets in database


if __name__ == '__main__':
    main()

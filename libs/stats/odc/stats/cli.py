import click
import json
from tqdm.auto import tqdm
import sys
import pickle
import pandas as pd
from typing import Dict, Tuple, List
from datetime import datetime
from collections import namedtuple
from odc.io.text import click_range2d
from datacube.utils.dates import normalise_dt
from ._cli_common import main
from . import _pq_cli

CompressedDataset = namedtuple("CompressedDataset", ['id', 'time'])


def is_tile_in(tidx, tiles):
    (x0, x1), (y0, y1) = tiles
    x, y = tidx
    return (x0 <= x < x1) and (y0 <= y < y1)

@main.command('save-tasks')
@click.option('--grid',
              type=str,
              help=("Grid name or spec: albers_au_25,albers_africa_{10|20|30|60},"
                    "'crs;pixel_resolution;shape_in_pixels'"),
              prompt="""Enter GridSpec
 one of albers_au_25, albers_africa_{10|20|30|60}
 or custom like 'epsg:3857;30;5000' (30m pixels 5,000 per side in epsg:3857)
 >""",
              default=None)
@click.option('--year',
              type=int,
              help="Only extract datasets for a given year. This is a shortcut for --temporal-range=<int>--P1Y")
@click.option('--temporal_range',
              type=str,
              help="Only extract datasets for a given time temporal_range, Example '2020-05--P1M' month of May 2020")
@click.option('--period',
              type=str,
              help="Extract datasets for a given period specified, Example 'all', 'annual', '2020-03--P2M' ")
@click.option('--env', '-E', type=str, help='Datacube environment name')
@click.option('-z', 'complevel',
              type=int,
              default=6,
              help='Compression setting for zstandard 1-fast, 9+ good but slow')
@click.option('--overwrite',
              is_flag=True,
              default=False,
              help='Overwrite output if it exists')
@click.option('--tiles',
              help='Limit query to tiles example: "0:3,2:4"',
              callback=click_range2d)
@click.option('--debug',
              is_flag=True,
              default=False,
              hidden=True,
              help='Dump debug data to pickle')
@click.argument('product', type=str, nargs=1)
@click.argument('output', type=str, nargs=1, default='')
def save_tasks(grid, year, temporal_range, period,
               output, product, env, complevel,
               overwrite=False,
               tiles=None,
               debug=False):
    """
    Prepare tasks for processing (query db).

    <todo more help goes here>

    \b
    Not yet implemented features:
      - output product config
      - multi-product inputs

    """
    import toolz
    from odc.index import chopped_dss, bin_dataset_stream, dataset_count, all_datasets
    from odc.dscache import create_cache, db_exists
    from odc.dscache.tools import dictionary_from_product_list
    from odc.dscache.tools.tiling import parse_gridspec_with_name
    from odc.dscache.tools.profiling import ds_stream_test_func
    from datacube import Datacube
    from datacube.model import Dataset
    from datacube.utils.geometry import Geometry
    from datacube.utils.documents import transform_object_tree
    from .metadata import gs_bounds, compute_grid_info, gjson_from_tasks
    from .model import DateTimeRange

    if temporal_range is not None and year is not None:
        print("Can only supply one of --year or --temporal_range", file=sys.stderr)
        sys.exit(1)

    if temporal_range is not None:
        try:
            temporal_range = DateTimeRange(temporal_range)
        except ValueError:
            print(f"Failed to parse supplied temporal_range: '{temporal_range}'")
            sys.exit(1)

    if year is not None:
        temporal_range = DateTimeRange.year(year)

    if period is not None:
        try:
            period = DateTimeRange(period) if not period in ['all' , 'annual'] else period
        except ValueError:
            print(f"Failed to parse supplied period: '{period}'")
            sys.exit(1)

    if output == '':
        if temporal_range is not None:
            output = f'{product}_{temporal_range.short}.db'
        else:
            output = f'{product}_all.db'

    def bin_data(dss: Dict[Tuple, list]) -> Dict[Tuple, List]:
        ids = []
        timestamps = []
        tidxs = []
        datasets = []

        # Read all the data
        for tidx, cell in cells.items():
            ids.extend([dss.id for dss in cell.dss])
            timestamps.extend([normalise_dt(dss.time) for dss in cell.dss])
            datasets.extend(dss for dss in cell.dss)
            tidxs.extend([tidx] * len(cell.dss))

        # Find min and max timestamps in the dataset(s)
        start = min(timestamps)
        end = max(timestamps)

        # Put all the data in one bin
        if period == 'all':
            bin = (f"{start.year}--P{end.year - start.year + 1}Y",)
            tasks = {bin + tidxs[0]: datasets}

        # Seasonal binning
        # 1900 is a dummy start year for the case where start year is %Y
        elif period is not None and period.start.year == 1900:
            tasks = {}
            anchor = period.start.month
            df = pd.DataFrame({"id": ids, "timestamp": timestamps, "tidx": tidxs, 'ds': datasets})
            for dt in tqdm(timestamps):
                df['bin'] = find_seasonal_bin(dt, period.freq, anchor).short

            for k, v in df.groupby(['tidx', "bin"]):
                tasks[(k[1],)+ k[0]] = v['ds']
        else:
            # Annual binning, this is the default
            tasks = {}
            for tidx, cell in cells.items():
                # TODO: deal with UTC offsets for day boundary determination
                grouped = toolz.groupby(lambda ds: ds.time.year, cell.dss)
                for year, dss in grouped.items():
                    temporal_k = (f"{year}--P1Y",)
                    tasks[temporal_k + tidx] = dss
        return tasks

    def find_seasonal_bin(dt: datetime, freq: int, anchor: int) -> DateTimeRange:
        dtr = DateTimeRange(f"{dt.year}-{anchor}--P{freq}")
        if dt in dtr:
            return dtr
        step = 1 if dt > dtr else -1
        while dt not in dtr:
            dtr = dtr + step
        return dtr

    def compress_ds(ds: Dataset) -> CompressedDataset:
        return CompressedDataset(ds.id, ds.center_time)

    def out_path(suffix, base=output):
        if base.endswith(".db"):
            base = base[:-3]
        return base + suffix

    def sanitize_query(query):
        def sanitize(v):
            if isinstance(v, Geometry):
                return v.json
            if isinstance(v, datetime):
                return v.isoformat()
            return v
        return transform_object_tree(sanitize, query)

    try:
        grid, gridspec = parse_gridspec_with_name(grid)
    except ValueError:
        print(f"""Failed to recognize/parse gridspec: '{grid}'
  Try one of the named ones: albers_au_25, albers_africa_{10|20|30|60}
  or define custom 'crs:3857;30;5000' - 30m pixels 5,000 pixels per side""", file=sys.stderr)
        sys.exit(2)

    cfg = dict(
        grid=grid,
        freq='1Y',
    )

    query = dict(product=product)

    if tiles is not None:
        (x0, x1), (y0, y1) = tiles
        print(f"Limit search to tiles: x:[{x0}, {x1}) y:[{y0}, {y1})")
        cfg['tiles'] = tiles
        query['geopolygon'] = gs_bounds(gridspec, tiles)

    # TODO: properly handle UTC offset when limiting query to a given time temporal_range
    #       Basically need to pad query by 12hours, then trim datasets post-query
    if temporal_range is not None:
        query.update(temporal_range.dc_query())
        cfg['temporal_range'] = temporal_range.short

    if db_exists(output) and overwrite is False:
        print(f"File database already exists: {output}, use --overwrite flag to force deletion", file=sys.stderr)
        sys.exit(1)

    print(f"Will write to {output}")

    cfg['query'] = sanitize_query(query)

    dc = Datacube(env=env)

    print("Connecting to the database, counting datasets")
    n_dss = dataset_count(dc.index, **query)
    if n_dss == 0:
        print("Found no datasets to process")
        sys.exit(3)

    print(f"Processing {n_dss:,d} datasets")

    print("Training compression dictionary")
    zdict = dictionary_from_product_list(dc, [product], samples_per_product=100)
    print(".. done")

    cache = create_cache(output, zdict=zdict, complevel=complevel, truncate=overwrite)
    cache.add_grid(gridspec, grid)

    cache.append_info_dict("stats/", dict(config=cfg))

    cells = {}
    if 'time' in query:
        dss = chopped_dss(dc, freq='w', **query)
    else:
        if len(query) == 1:
            dss = all_datasets(dc, **query)
        else:
            # note: this blocks for large result sets
            dss = dc.find_datasets_lazy(**query)

    dss = cache.tee(dss)
    dss = bin_dataset_stream(gridspec, dss, cells, persist=compress_ds)
    dss = tqdm(dss, total=n_dss)

    rr = ds_stream_test_func(dss)
    print(rr.text)

    if tiles is not None:
        # prune out tiles that were not requested
        cells = {tidx: dss
                 for tidx, dss in cells.items()
                 if is_tile_in(tidx, tiles)}

    n_tiles = len(cells)
    print(f"Total of {n_tiles:,d} spatial tiles")

    tasks = bin_data(cells)

    if temporal_range is not None:
        # TODO: deal with UTC offsets for day boundary determination and trim
        # datasets that fall outside of requested time period
        temporal_k = (temporal_range.short,)
        tasks = {temporal_k + k: x.dss
                 for k, x in cells.items()}

    tasks_uuid = {k: [ds.id for ds in dss]
                  for k, dss in tasks.items()}

    print(f"Saving tasks to disk ({len(tasks)})")
    cache.add_grid_tiles(grid, tasks_uuid)
    print(".. done")

    csv_path = out_path(".csv")
    print(f"Writing summary to {csv_path}")
    with open(csv_path, 'wt') as f:
        f.write('"temporal_range","X","Y","datasets","days"\n')

        for p, x, y in sorted(tasks):
            dss = tasks[(p, x, y)]
            n_dss = len(dss)
            n_days = len(set(ds.time.date() for ds in dss))
            line = f'"{p}", {x:+05d}, {y:+05d}, {n_dss:4d}, {n_days:4d}\n'
            f.write(line)

    print("Dumping GeoJSON(s)")
    grid_info = compute_grid_info(cells,
                                  resolution=max(gridspec.tile_size)/4)
    tasks_geo = gjson_from_tasks(tasks, grid_info)
    for temporal_range, gjson in tasks_geo.items():
        fname = out_path(f'-{temporal_range}.geojson')
        print(f"..writing to {fname}")
        with open(fname, 'wt') as f:
            json.dump(gjson, f)

    if debug:
        pkl_path = out_path('-cells.pkl')
        print(f"Saving debug info to: {pkl_path}")
        with open(pkl_path, "wb") as f:
            pickle.dump(cells, f)

        pkl_path = out_path('-tasks.pkl')
        print(f"Saving debug info to: {pkl_path}")
        with open(pkl_path, "wb") as f:
            pickle.dump(tasks, f)

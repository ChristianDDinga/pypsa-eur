#!/usr/bin/env python
# -*- coding: utf-8 -*-

# SPDX-FileCopyrightText: : 2017-2023 The PyPSA-Eur Authors
#
# SPDX-License-Identifier: MIT
"""
Calculates for each network node the (i) installable capacity (based on land-
use), (ii) the available generation time series (based on weather data), and
(iii) the average distance from the node for onshore wind, AC-connected
offshore wind, DC-connected offshore wind and solar PV generators. In addition
for offshore wind it calculates the fraction of the grid connection which is
under water.

.. note:: Hydroelectric profiles are built in script :mod:`build_hydro_profiles`.

Relevant settings
-----------------

.. code:: yaml

    snapshots:

    atlite:
        nprocesses:

    renewable:
        {technology}:
            cutout: corine: luisa: grid_codes: distance: natura: max_depth:
            max_shore_distance: min_shore_distance: capacity_per_sqkm:
            correction_factor: min_p_max_pu: clip_p_max_pu: resource:

.. seealso::
    Documentation of the configuration file ``config/config.yaml`` at
    :ref:`snapshots_cf`, :ref:`atlite_cf`, :ref:`renewable_cf`

Inputs
------

- ``data/bundle/corine/g250_clc06_V18_5.tif``: `CORINE Land Cover (CLC)
  <https://land.copernicus.eu/pan-european/corine-land-cover>`_ inventory on `44
  classes <https://wiki.openstreetmap.org/wiki/Corine_Land_Cover#Tagging>`_ of
  land use (e.g. forests, arable land, industrial, urban areas) at 100m
  resolution.

    .. image:: img/corine.png
        :scale: 33 %

- ``data/LUISA_basemap_020321_50m.tif``: `LUISA Base Map
  <https://publications.jrc.ec.europa.eu/repository/handle/JRC124621>`_ land
  coverage dataset at 50m resolution similar to CORINE. For codes in relation to
  CORINE land cover, see `Annex 1 of the technical documentation
  <https://publications.jrc.ec.europa.eu/repository/bitstream/JRC124621/technical_report_luisa_basemap_2018_v7_final.pdf>`_.

- ``data/bundle/GEBCO_2014_2D.nc``: A `bathymetric
  <https://en.wikipedia.org/wiki/Bathymetry>`_ data set with a global terrain
  model for ocean and land at 15 arc-second intervals by the `General
  Bathymetric Chart of the Oceans (GEBCO)
  <https://www.gebco.net/data_and_products/gridded_bathymetry_data/>`_.

    .. image:: img/gebco_2019_grid_image.jpg
        :scale: 50 %

    **Source:** `GEBCO
    <https://www.gebco.net/data_and_products/images/gebco_2019_grid_image.jpg>`_

- ``resources/natura.tiff``: confer :ref:`natura`
- ``resources/offshore_shapes.geojson``: confer :ref:`shapes`
- ``resources/regions_onshore.geojson``: (if not offshore wind), confer
  :ref:`busregions`
- ``resources/regions_offshore.geojson``: (if offshore wind), :ref:`busregions`
- ``"cutouts/" + params["renewable"][{technology}]['cutout']``: :ref:`cutout`
- ``networks/base.nc``: :ref:`base`

Outputs
-------

- ``resources/profile_{technology}.nc`` with the following structure

    ===================  ==========  =========================================================
    Field                Dimensions  Description
    ===================  ==========  =========================================================
    profile              bus, time   the per unit hourly availability factors for each node
    -------------------  ----------  ---------------------------------------------------------
    weight               bus         sum of the layout weighting for each node
    -------------------  ----------  ---------------------------------------------------------
    p_nom_max            bus         maximal installable capacity at the node (in MW)
    -------------------  ----------  ---------------------------------------------------------
    potential            y, x        layout of generator units at cutout grid cells inside the
                                     Voronoi cell (maximal installable capacity at each grid
                                     cell multiplied by capacity factor)
    -------------------  ----------  ---------------------------------------------------------
    average_distance     bus         average distance of units in the Voronoi cell to the
                                     grid node (in km)
    -------------------  ----------  ---------------------------------------------------------
    underwater_fraction  bus         fraction of the average connection distance which is
                                     under water (only for offshore)
    ===================  ==========  =========================================================

    - **profile**

    .. image:: img/profile_ts.png
        :scale: 33 %
        :align: center

    - **p_nom_max**

    .. image:: img/p_nom_max_hist.png
        :scale: 33 %
        :align: center

    - **potential**

    .. image:: img/potential_heatmap.png
        :scale: 33 %
        :align: center

    - **average_distance**

    .. image:: img/distance_hist.png
        :scale: 33 %
        :align: center

    - **underwater_fraction**

    .. image:: img/underwater_hist.png
        :scale: 33 %
        :align: center

Description
-----------

This script functions at two main spatial resolutions: the resolution of the
network nodes and their `Voronoi cells
<https://en.wikipedia.org/wiki/Voronoi_diagram>`_, and the resolution of the
cutout grid cells for the weather data. Typically the weather data grid is finer
than the network nodes, so we have to work out the distribution of generators
across the grid cells within each Voronoi cell. This is done by taking account
of a combination of the available land at each grid cell and the capacity factor
there.

First the script computes how much of the technology can be installed at each
cutout grid cell and each node using the `atlite
<https://github.com/pypsa/atlite>`_ library. This uses the CORINE land use data,
LUISA land use data, Natura2000 nature reserves, GEBCO bathymetry data, and
shipping lanes.

.. image:: img/eligibility.png
    :scale: 50 %
    :align: center

To compute the layout of generators in each node's Voronoi cell, the installable
potential in each grid cell is multiplied with the capacity factor at each grid
cell. This is done since we assume more generators are installed at cells with a
higher capacity factor.

.. image:: img/offwinddc-gridcell.png
    :scale: 50 %
    :align: center

.. image:: img/offwindac-gridcell.png
    :scale: 50 %
    :align: center

.. image:: img/onwind-gridcell.png
    :scale: 50 %
    :align: center

.. image:: img/solar-gridcell.png
    :scale: 50 %
    :align: center

This layout is then used to compute the generation availability time series from
the weather data cutout from ``atlite``.

The maximal installable potential for the node (`p_nom_max`) is computed by
adding up the installable potentials of the individual grid cells. If the model
comes close to this limit, then the time series may slightly overestimate
production since it is assumed the geographical distribution is proportional to
capacity factor.
"""
import functools
import logging
import time

import atlite
import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from _helpers import configure_logging
from dask.distributed import Client
from pypsa.geo import haversine
from shapely.geometry import LineString

logger = logging.getLogger(__name__)


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("build_renewable_profiles", technology="solar")
    configure_logging(snakemake)

    nprocesses = int(snakemake.threads)
    noprogress = snakemake.config["run"].get("disable_progressbar", True)
    noprogress = noprogress or not snakemake.config["atlite"]["show_progress"]
    params = snakemake.params.renewable[snakemake.wildcards.technology]
    resource = params["resource"]  # pv panel params / wind turbine params
    correction_factor = params.get("correction_factor", 1.0)
    capacity_per_sqkm = params["capacity_per_sqkm"]
    snapshots = snakemake.params.snapshots

    if correction_factor != 1.0:
        logger.info(f"correction_factor is set as {correction_factor}")

    if nprocesses > 1:
        client = Client(n_workers=nprocesses, threads_per_worker=1)
    else:
        client = None

    sns = pd.date_range(freq="h", **snapshots)
    cutout = atlite.Cutout(snakemake.input.cutout).sel(time=sns)
    regions = gpd.read_file(snakemake.input.regions)
    assert not regions.empty, (
        f"List of regions in {snakemake.input.regions} is empty, please "
        "disable the corresponding renewable technology"
    )
    # do not pull up, set_index does not work if geo dataframe is empty
    regions = regions.set_index("name").rename_axis("bus")
    buses = regions.index

    res = params.get("excluder_resolution", 100)
    excluder = atlite.ExclusionContainer(crs=3035, res=res)

    if params["natura"]:
        excluder.add_raster(snakemake.input.natura, nodata=0, allow_no_overlap=True)

    for dataset in ["corine", "luisa"]:
        kwargs = {"nodata": 0} if dataset == "luisa" else {}
        settings = params.get(dataset, {})
        if not settings:
            continue
        if dataset == "luisa" and res > 50:
            logger.info(
                "LUISA data is available at 50m resolution, "
                f"but coarser {res}m resolution is used."
            )
        if isinstance(settings, list):
            settings = {"grid_codes": settings}
        if "grid_codes" in settings:
            codes = settings["grid_codes"]
            excluder.add_raster(
                snakemake.input[dataset], codes=codes, invert=True, crs=3035, **kwargs
            )
        if settings.get("distance", 0.0) > 0.0:
            codes = settings["distance_grid_codes"]
            buffer = settings["distance"]
            excluder.add_raster(
                snakemake.input[dataset], codes=codes, buffer=buffer, crs=3035, **kwargs
            )

    if params.get("ship_threshold"):
        shipping_threshold = (
            params["ship_threshold"] * 8760 * 6
        )  # approximation because 6 years of data which is hourly collected
        func = functools.partial(np.less, shipping_threshold)
        excluder.add_raster(
            snakemake.input.ship_density, codes=func, crs=4326, allow_no_overlap=True
        )

    if params.get("max_depth"):
        # lambda not supported for atlite + multiprocessing
        # use named function np.greater with partially frozen argument instead
        # and exclude areas where: -max_depth > grid cell depth
        func = functools.partial(np.greater, -params["max_depth"])
        excluder.add_raster(snakemake.input.gebco, codes=func, crs=4326, nodata=-1000)

    if "min_shore_distance" in params:
        buffer = params["min_shore_distance"]
        excluder.add_geometry(snakemake.input.country_shapes, buffer=buffer)

    if "max_shore_distance" in params:
        buffer = params["max_shore_distance"]
        excluder.add_geometry(
            snakemake.input.country_shapes, buffer=buffer, invert=True
        )

    logger.info("Calculate landuse availability...")
    start = time.time()

    kwargs = dict(nprocesses=nprocesses, disable_progressbar=noprogress)
    availability = cutout.availabilitymatrix(regions, excluder, **kwargs)

    duration = time.time() - start
    logger.info(f"Completed landuse availability calculation ({duration:2.2f}s)")

    # For Moldova and Ukraine: Overwrite parts not covered by Corine with
    # externally determined available areas
    if "availability_matrix_MD_UA" in snakemake.input.keys():
        availability_MDUA = xr.open_dataarray(
            snakemake.input["availability_matrix_MD_UA"]
        )
        availability.loc[availability_MDUA.coords] = availability_MDUA

    area = cutout.grid.to_crs(3035).area / 1e6
    area = xr.DataArray(
        area.values.reshape(cutout.shape), [cutout.coords["y"], cutout.coords["x"]]
    )

    potential = capacity_per_sqkm * availability.sum("bus") * area
    func = getattr(cutout, resource.pop("method"))
    if client is not None:
        resource["dask_kwargs"] = {"scheduler": client}

    logger.info("Calculate average capacity factor...")
    start = time.time()

    capacity_factor = correction_factor * func(capacity_factor=True, **resource)
    layout = capacity_factor * area * capacity_per_sqkm

    duration = time.time() - start
    logger.info(f"Completed average capacity factor calculation ({duration:2.2f}s)")

    logger.info("Calculate weighted capacity factor time series...")
    start = time.time()

    profile, capacities = func(
        matrix=availability.stack(spatial=["y", "x"]),
        layout=layout,
        index=buses,
        per_unit=True,
        return_capacity=True,
        **resource,
    )

    duration = time.time() - start
    logger.info(
        f"Completed weighted capacity factor time series calculation ({duration:2.2f}s)"
    )

    logger.info(f"Calculating maximal capacity per bus")
    p_nom_max = capacity_per_sqkm * availability @ area

    logger.info("Calculate average distances.")
    layoutmatrix = (layout * availability).stack(spatial=["y", "x"])

    coords = cutout.grid[["x", "y"]]
    bus_coords = regions[["x", "y"]]

    average_distance = []
    centre_of_mass = []
    for bus in buses:
        row = layoutmatrix.sel(bus=bus).data
        nz_b = row != 0
        row = row[nz_b]
        co = coords[nz_b]
        distances = haversine(bus_coords.loc[bus], co)
        average_distance.append((distances * (row / row.sum())).sum())
        centre_of_mass.append(co.values.T @ (row / row.sum()))

    average_distance = xr.DataArray(average_distance, [buses])
    centre_of_mass = xr.DataArray(centre_of_mass, [buses, ("spatial", ["x", "y"])])

    ds = xr.merge(
        [
            (correction_factor * profile).rename("profile"),
            capacities.rename("weight"),
            p_nom_max.rename("p_nom_max"),
            potential.rename("potential"),
            average_distance.rename("average_distance"),
        ]
    )

    if snakemake.wildcards.technology.startswith("offwind"):
        logger.info("Calculate underwater fraction of connections.")
        offshore_shape = gpd.read_file(snakemake.input["offshore_shapes"]).unary_union
        underwater_fraction = []
        for bus in buses:
            p = centre_of_mass.sel(bus=bus).data
            line = LineString([p, regions.loc[bus, ["x", "y"]]])
            frac = line.intersection(offshore_shape).length / line.length
            underwater_fraction.append(frac)

        ds["underwater_fraction"] = xr.DataArray(underwater_fraction, [buses])

    # select only buses with some capacity and minimal capacity factor
    ds = ds.sel(
        bus=(
            (ds["profile"].mean("time") > params.get("min_p_max_pu", 0.0))
            & (ds["p_nom_max"] > params.get("min_p_nom_max", 0.0))
        )
    )

    if "clip_p_max_pu" in params:
        min_p_max_pu = params["clip_p_max_pu"]
        ds["profile"] = ds["profile"].where(ds["profile"] >= min_p_max_pu, 0)

    ds.to_netcdf(snakemake.output.profile)

    if client is not None:
        client.shutdown()

# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: : 2020-2024 The PyPSA-Eur and PyPSA-Earth Authors
#
# SPDX-License-Identifier: MIT

import logging
import os
import string

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from _helpers import configure_logging, set_scenario_config
from itertools import chain
from shapely.geometry import LineString, Point
from shapely.ops import linemerge, nearest_points, split
from shapely.algorithms.polylabel import polylabel
from tqdm import tqdm

logger = logging.getLogger(__name__)


GEO_CRS = "EPSG:4326"
DISTANCE_CRS = "EPSG:3035"
BUS_TOL = (
    500  # unit: meters, default 5000 - Buses within this distance are grouped together
)
LINES_COLUMNS = [
    "bus0",
    "bus1",
    "voltage",
    "circuits",
    "length",
    "underground",
    "under_construction",
    "geometry",
]
LINKS_COLUMNS = [
    "bus0",
    "bus1",
    "voltage",
    "p_nom",
    "length",
    "underground",
    "under_construction",
    "geometry",
]
TRANSFORMERS_COLUMNS = [
    "bus0",
    "bus1",
    "voltage_bus0",
    "voltage_bus1",
    "country",
    "geometry",
]


def line_endings_to_bus_conversion(lines):
    """
    Converts line endings to bus connections.

    This function takes a df of lines and converts the line endings to bus
    connections. It performs the necessary operations to ensure that the line
    endings are properly connected to the buses in the network.

    Parameters:
    lines (DataFrame)

    Returns:
    lines (DataFrame)
    """
    lines = lines.copy()
    # Assign to every line a start and end point

    lines["bounds"] = lines["geometry"].boundary  # create start and end point

    lines["bus_0_coors"] = lines["bounds"].map(lambda p: p.geoms[0])
    lines["bus_1_coors"] = lines["bounds"].map(lambda p: p.geoms[-1])

    # splits into coordinates
    lines["bus0_lon"] = lines["bus_0_coors"].x
    lines["bus0_lat"] = lines["bus_0_coors"].y
    lines["bus1_lon"] = lines["bus_1_coors"].x
    lines["bus1_lat"] = lines["bus_1_coors"].y

    return lines


# tol in m
def set_substations_ids(buses, distance_crs, tol=BUS_TOL, prefix=""):
    """
    Function to set substations ids to buses, accounting for location
    tolerance.

    The algorithm is as follows:

    1. initialize all substation ids to -1
    2. if the current substation has been already visited [substation_id < 0], then skip the calculation
    3. otherwise:
        1. identify the substations within the specified tolerance (tol)
        2. when all the substations in tolerance have substation_id < 0, then specify a new substation_id
        3. otherwise, if one of the substation in tolerance has a substation_id >= 0, then set that substation_id to all the others;
           in case of multiple substations with substation_ids >= 0, the first value is picked for all
    """
    buses = buses.copy()
    buses["station_id"] = -1

    # create temporary series to execute distance calculations using m as reference distances
    temp_bus_geom = buses.geometry.to_crs(distance_crs)

    # set tqdm options for substation ids
    tqdm_kwargs_substation_ids = dict(
        ascii=False,
        unit=" buses",
        total=buses.shape[0],
        desc="Set substation ids ",
    )
    
    station_id = 0
    for i, row in tqdm(buses.iterrows(), **tqdm_kwargs_substation_ids):
        if buses.loc[i, "station_id"] >= 0:
            continue

        # get substations within tolerance
        close_nodes = np.flatnonzero(
            temp_bus_geom.distance(temp_bus_geom.loc[i]) <= tol
        )

        if len(close_nodes) == 1:
            # if only one substation is in tolerance, then the substation is the current one iì
            # Note that the node cannot be with substation_id >= 0, given the preliminary check
            # at the beginning of the for loop
            buses.loc[buses.index[i], "station_id"] = station_id
            # update station id
            station_id += 1
        else:
            # several substations in tolerance
            # get their ids
            subset_substation_ids = buses.loc[buses.index[close_nodes], "station_id"]
            # check if all substation_ids are negative (<0)
            all_neg = subset_substation_ids.max() < 0
            # check if at least a substation_id is negative (<0)
            some_neg = subset_substation_ids.min() < 0

            if all_neg:
                # when all substation_ids are negative, then this is a new substation id
                # set the current station_id and increment the counter
                buses.loc[buses.index[close_nodes], "station_id"] = station_id
                station_id += 1
            elif some_neg:
                # otherwise, when at least a substation_id is non-negative, then pick the first value
                # and set it to all the other substations within tolerance
                sub_id = -1
                for substation_id in subset_substation_ids:
                    if substation_id >= 0:
                        sub_id = substation_id
                        break
                buses.loc[buses.index[close_nodes], "station_id"] = sub_id
    
    # Add prefix to station_id
    buses["station_id"] = prefix + buses["station_id"].astype(str)

    return buses


def set_lines_ids(lines, buses, distance_crs):
    """
    Function to set line buses ids to the closest bus in the list.
    """
    # set tqdm options for set lines ids
    tqdm_kwargs_line_ids = dict(
        ascii=False,
        unit=" lines",
        total=lines.shape[0],
        desc="Set line/link bus ids ",
    )

    # initialization
    lines["bus0"] = -1
    lines["bus1"] = -1

    busesepsg = buses.to_crs(distance_crs)
    linesepsg = lines.to_crs(distance_crs)

    for i, row in tqdm(linesepsg.iterrows(), **tqdm_kwargs_line_ids):
        # select buses having the voltage level of the current line
        buses_sel = busesepsg[
            (buses["voltage"] == row["voltage"]) & (buses["dc"] == row["dc"])
        ]

        # find the closest node of the bus0 of the line
        bus0_id = buses_sel.geometry.distance(row.geometry.boundary.geoms[0]).idxmin()
        lines.loc[i, "bus0"] = buses.loc[bus0_id, "bus_id"]

        # check if the line starts exactly in the node, otherwise modify the linestring
        distance_bus0 = busesepsg.geometry.loc[bus0_id].distance(
            row.geometry.boundary.geoms[0]
        )

        if distance_bus0 > 0:
            # the line does not start in the node, thus modify the linestring
            line_start_point = lines.geometry.loc[i].boundary.geoms[0]
            new_segment = LineString([buses.geometry.loc[bus0_id], line_start_point])
            modified_line = linemerge([new_segment, lines.geometry.loc[i]])
            lines.loc[i, "geometry"] = modified_line

        # find the closest node of the bus1 of the line
        bus1_id = buses_sel.geometry.distance(row.geometry.boundary.geoms[1]).idxmin()
        lines.loc[i, "bus1"] = buses.loc[bus1_id, "bus_id"]

        # check if the line ends exactly in the node, otherwise modify the linestring
        distance_bus1 = busesepsg.geometry.loc[bus1_id].distance(
            row.geometry.boundary.geoms[1]
        )

        if distance_bus1 > 0:
            # the line does not start in the node, thus modify the linestring
            line_end_point = lines.geometry.loc[i].boundary.geoms[1]
            new_segment = LineString([line_end_point, buses.geometry.loc[bus1_id]])
            modified_line = linemerge([lines.geometry.loc[i], new_segment])
            lines.loc[i, "geometry"] = modified_line

    return lines, buses


def merge_stations_same_station_id(
    buses, delta_lon=0.0001, delta_lat=0.0001, precision=4
):
    """
    Function to merge buses with same voltage and station_id This function
    iterates over all substation ids and creates a bus_id for every substation
    and voltage level.

    Therefore, a substation with multiple voltage levels is represented
    with different buses, one per voltage level
    """
    # initialize list of cleaned buses
    buses_clean = []

    # initialize the number of buses
    n_buses = 0

    for g_name, g_value in buses.groupby(by="station_id"):
        station_point_x = np.round(g_value.geometry.x.mean(), precision)
        station_point_y = np.round(g_value.geometry.y.mean(), precision)
        # is_dclink_boundary_point = any(g_value["is_dclink_boundary_point"])

        # loop for every voltage level in the bus
        # The location of the buses is averaged; in the case of multiple voltage levels for the same station_id,
        # each bus corresponding to a voltage level and each polatity is located at a distance regulated by delta_lon/delta_lat
        v_it = 0
        for v_name, bus_row in g_value.groupby(by=["voltage", "country", "dc"]):
            lon_bus = np.round(station_point_x + v_it * delta_lon, precision)
            lat_bus = np.round(station_point_y + v_it * delta_lat, precision)

            bus_data = [
                n_buses,  # "bus_id"
                g_name,  # "station_id"
                v_name[0],  # "voltage"
                bus_row["dc"].all(),  # "dc"
                "|".join(bus_row["symbol"].unique()),  # "symbol"
                bus_row["under_construction"].any(),  # "under_construction"
                "|".join(bus_row["tag_substation"].unique()),  # "tag_substation"
                bus_row["tag_area"].sum(),  # "tag_area"
                lon_bus,  # "lon"
                lat_bus,  # "lat"
                bus_row["country"].iloc[0],  # "country"
                Point(lon_bus, lat_bus),  # "geometry"
            ]

            # add the bus
            buses_clean.append(bus_data)

            # increase counters
            v_it += 1
            n_buses += 1

    # names of the columns
    buses_clean_columns = [
        "bus_id",
        "station_id",
        "voltage",
        "dc",
        "symbol",
        "under_construction",
        "tag_substation",
        "tag_area",
        "x",
        "y",
        "country",
        # "is_dclink_boundary_point",
        "geometry",
    ]

    gdf_buses_clean = gpd.GeoDataFrame(
        buses_clean, columns=buses_clean_columns
    ).set_crs(crs=buses.crs, inplace=True)

    return gdf_buses_clean


def get_ac_frequency(df, fr_col="tag_frequency"):
    """
    # Function to define a default frequency value.

    Attempts to find the most usual non-zero frequency across the
    dataframe; 50 Hz is assumed as a back-up value
    """

    # Initialize a default frequency value
    ac_freq_default = 50

    grid_freq_levels = df[fr_col].value_counts(sort=True, dropna=True)
    if not grid_freq_levels.empty:
        # AC lines frequency shouldn't be 0Hz
        ac_freq_levels = grid_freq_levels.loc[
            grid_freq_levels.index.get_level_values(0) != "0"
        ]
        ac_freq_default = ac_freq_levels.index.get_level_values(0)[0]

    return ac_freq_default


def get_transformers(buses, lines):
    """
    Function to create fake transformer lines that connect buses of the same
    station_id at different voltage.
    """

    ac_freq = get_ac_frequency(lines)
    df_transformers = []

    # Transformers should be added between AC buses only
    buses_ac = buses[buses["dc"] != True]

    for g_name, g_value in buses_ac.sort_values("voltage", ascending=True).groupby(
        by="station_id"
    ):
        # note: by construction there cannot be more that two buses with the same station_id and same voltage
        n_voltages = len(g_value)

        if n_voltages > 1:
            for id in range(n_voltages - 1):
                # when g_value has more than one node, it means that there are multiple voltages for the same bus
                transformer_geometry = LineString(
                    [g_value.geometry.iloc[id], g_value.geometry.iloc[id + 1]]
                )

                transformer_data = [
                    f"transf_{g_name}_{id}",  # "line_id"
                    g_value["bus_id"].iloc[id],  # "bus0"
                    g_value["bus_id"].iloc[id + 1],  # "bus1"
                    g_value.voltage.iloc[id],  # "voltage_bus0"
                    g_value.voltage.iloc[id + 1],  # "voltage_bus0"
                    g_value.country.iloc[id],  # "country"
                    transformer_geometry,  # "geometry"
                ]

                df_transformers.append(transformer_data)

    # name of the columns
    transformers_columns = [
        "transformer_id",
        "bus0",
        "bus1",
        "voltage_bus0",
        "voltage_bus1",
        "country",
        "geometry",
    ]

    df_transformers = gpd.GeoDataFrame(df_transformers, columns=transformers_columns)
    if not df_transformers.empty:
        init_index = 0 if lines.empty else lines.index[-1] + 1
        df_transformers.set_index(init_index + df_transformers.index, inplace=True)
    # update line endings
    df_transformers = line_endings_to_bus_conversion(df_transformers)
    df_transformers.drop(columns=["bounds", "bus_0_coors", "bus_1_coors"], inplace=True)

    gdf_transformers = gpd.GeoDataFrame(df_transformers)
    gdf_transformers.crs = GEO_CRS

    return gdf_transformers


def _find_closest_bus(row, buses, distance_crs, tol=BUS_TOL):
    """
    Find the closest bus to a given bus based on geographical distance and
    country.

    Parameters:
    - row: The bus_id of the bus to find the closest bus for.
    - buses: A GeoDataFrame containing information about all the buses.
    - distance_crs: The coordinate reference system to use for distance calculations.
    - tol: The tolerance distance within which a bus is considered closest (default: 5000).
    Returns:
    - closest_bus_id: The bus_id of the closest bus, or None if no bus is found within the distance and same country.
    """
    gdf_buses = buses.copy()
    gdf_buses = gdf_buses.to_crs(distance_crs)
    # Get the geometry of the bus with bus_id = link_bus_id
    bus = gdf_buses[gdf_buses["bus_id"] == row]
    bus_geom = bus.geometry.values[0]

    gdf_buses_filtered = gdf_buses[gdf_buses["dc"] == False]

    # Find the closest point in the filtered buses
    nearest_geom = nearest_points(bus_geom, gdf_buses_filtered.union_all())[1]

    # Get the bus_id of the closest bus
    closest_bus = gdf_buses_filtered.loc[gdf_buses["geometry"] == nearest_geom]

    # check if closest_bus_id is within the distance
    within_distance = (
        closest_bus.to_crs(distance_crs).distance(bus.to_crs(distance_crs), align=False)
    ).values[0] <= tol

    in_same_country = closest_bus.country.values[0] == bus.country.values[0]

    if within_distance and in_same_country:
        closest_bus_id = closest_bus.bus_id.values[0]
    else:
        closest_bus_id = None

    return closest_bus_id


def _get_converters(buses, links, distance_crs):
    """
    Get the converters for the given buses and links. Connecting link endings
    to closest AC bus.

    Parameters:
    - buses (pandas.DataFrame): DataFrame containing information about buses.
    - links (pandas.DataFrame): DataFrame containing information about links.
    Returns:
    - gdf_converters (geopandas.GeoDataFrame): GeoDataFrame containing information about converters.
    """
    converters = []
    for idx, row in links.iterrows():
        for conv in range(2):
            link_end = row[f"bus{conv}"]
            # HVDC Gotland is connected to 130 kV grid, closest HVAC bus is further away

            closest_bus = _find_closest_bus(link_end, buses, distance_crs, tol=40000)

            if closest_bus is None:
                continue

            converter_id = f"converter/{row['link_id']}_{conv}"
            converter_geometry = LineString(
                [
                    buses[buses["bus_id"] == link_end].geometry.values[0],
                    buses[buses["bus_id"] == closest_bus].geometry.values[0],
                ]
            )

            logger.info(
                f"Added converter #{conv+1}/2 for link {row['link_id']}:{converter_id}."
            )

            converter_data = [
                converter_id,  # "line_id"
                link_end,  # "bus0"
                closest_bus,  # "bus1"
                row["voltage"],  # "voltage"
                row["p_nom"],  # "p_nom"
                False,  # "underground"
                False,  # "under_construction"
                buses[buses["bus_id"] == closest_bus].country.values[0],  # "country"
                converter_geometry,  # "geometry"
            ]

            # Create the converter
            converters.append(converter_data)

    conv_columns = [
        "converter_id",
        "bus0",
        "bus1",
        "voltage",
        "p_nom",
        "underground",
        "under_construction",
        "country",
        "geometry",
    ]

    gdf_converters = gpd.GeoDataFrame(
        converters, columns=conv_columns, crs=GEO_CRS
    ).reset_index()

    return gdf_converters


def connect_stations_same_station_id(lines, buses):
    """
    Function to create fake links between substations with the same
    substation_id.
    """
    ac_freq = get_ac_frequency(lines)
    station_id_list = buses.station_id.unique()

    add_lines = []
    from shapely.geometry import LineString

    for s_id in station_id_list:
        buses_station_id = buses[buses.station_id == s_id]

        if len(buses_station_id) > 1:
            for b_it in range(1, len(buses_station_id)):
                line_geometry = LineString(
                    [
                        buses_station_id.geometry.iloc[0],
                        buses_station_id.geometry.iloc[b_it],
                    ]
                )
                line_bounds = line_geometry.bounds

                line_data = [
                    f"link{buses_station_id}_{b_it}",  # "line_id"
                    buses_station_id.index[0],  # "bus0"
                    buses_station_id.index[b_it],  # "bus1"
                    400000,  # "voltage"
                    1,  # "circuits"
                    0.0,  # "length"
                    False,  # "underground"
                    False,  # "under_construction"
                    "transmission",  # "tag_type"
                    ac_freq,  # "tag_frequency"
                    buses_station_id.country.iloc[0],  # "country"
                    line_geometry,  # "geometry"
                    line_bounds,  # "bounds"
                    buses_station_id.geometry.iloc[0],  # "bus_0_coors"
                    buses_station_id.geometry.iloc[b_it],  # "bus_1_coors"
                    buses_station_id.lon.iloc[0],  # "bus0_lon"
                    buses_station_id.lat.iloc[0],  # "bus0_lat"
                    buses_station_id.lon.iloc[b_it],  # "bus1_lon"
                    buses_station_id.lat.iloc[b_it],  # "bus1_lat"
                ]

                add_lines.append(line_data)

    # name of the columns
    add_lines_columns = [
        "line_id",
        "bus0",
        "bus1",
        "voltage",
        "circuits",
        "length",
        "underground",
        "under_construction",
        "tag_type",
        "tag_frequency",
        "country",
        "geometry",
        "bounds",
        "bus_0_coors",
        "bus_1_coors",
        "bus0_lon",
        "bus0_lat",
        "bus1_lon",
        "bus1_lat",
    ]

    df_add_lines = gpd.GeoDataFrame(pd.concat(add_lines), columns=add_lines_columns)
    lines = pd.concat([lines, df_add_lines], ignore_index=True)

    return lines


def set_lv_substations(buses):
    """
    Function to set what nodes are lv, thereby setting substation_lv The
    current methodology is to set lv nodes to buses where multiple voltage
    level are found, hence when the station_id is duplicated.
    """
    # initialize column substation_lv to true
    buses["substation_lv"] = True

    # For each station number with multiple buses make lowest voltage `substation_lv = TRUE`
    bus_with_stations_duplicates = buses[
        buses.station_id.duplicated(keep=False)
    ].sort_values(by=["station_id", "voltage"])
    lv_bus_at_station_duplicates = (
        buses[buses.station_id.duplicated(keep=False)]
        .sort_values(by=["station_id", "voltage"])
        .drop_duplicates(subset=["station_id"])
    )
    # Set all buses with station duplicates "False"
    buses.loc[bus_with_stations_duplicates.index, "substation_lv"] = False
    # Set lv_buses with station duplicates "True"
    buses.loc[lv_bus_at_station_duplicates.index, "substation_lv"] = True

    return buses


def merge_stations_lines_by_station_id_and_voltage(
    lines, links, buses, countries, stations, distance_crs=DISTANCE_CRS, tol=BUS_TOL
):
    """
    Function to merge close buses to stations and adapt the line datasets to adhere to
    the merged dataset.
    """
    # Merge buses with same voltage and within tolerance
    logger.info(f"Aggregating close substations with a tolerance of {tol} m")
    # bus types (AC != DC)
    buses_all = buses.reset_index(drop=True).drop(columns=["station_id", "country"])

    # Set station ids based on station seeds (polygons)
    buses_all = gpd.sjoin(buses_all, stations, how="left", predicate="within")
    buses_all["geometry"] = buses_all["poi"]

    buses_all = buses_all.drop_duplicates(subset=["station_id", "voltage", "dc", "geometry", "country"])

    # merge buses with same station id and voltage
    logger.info(" - Merging substations with the same id")
    if not buses_all.empty:
        buses_all = merge_stations_same_station_id(buses_all)
    
    # Systematically update bus_id name
    buses_all["bus_id"] = buses_all["station_id"]
    buses_all["bus_id"] = buses_all["bus_id"] + "-" + buses_all["voltage"].div(1e3).astype(int).astype(str)
    # add suffix "DC" if dc column == True
    buses_all["bus_id"] = buses_all["bus_id"] + buses_all["dc"].apply(lambda x: "-DC" if x else "")
    buses_all.index = buses_all["bus_id"]

    logger.info(" - Specifying the bus ids of the line endings")


    #### CONTINUE HERE TODO

    # set the bus ids to the line dataset
    lines, buses_all = set_lines_ids(lines, buses_all, distance_crs)
    links, buses = set_lines_ids(links, buses, distance_crs)

    # drop lines starting and ending in the same node
    lines.drop(lines[lines["bus0"] == lines["bus1"]].index, inplace=True)
    links.drop(links[links["bus0"] == links["bus1"]].index, inplace=True)
    # update line endings
    lines = line_endings_to_bus_conversion(lines)
    links = line_endings_to_bus_conversion(links)

    # set substation_lv
    set_lv_substations(buses)

    # reset index
    lines.reset_index(drop=True, inplace=True)
    links.reset_index(drop=True, inplace=True)

    return lines, links, buses


def _split_linestring_by_point(linestring, points):
    """
    Function to split a linestring geometry by multiple inner points.

    Parameters
    ----------
    lstring : LineString
        Linestring of the line to be split
    points : list
        List of points to split the linestring

    Return
    ------
    list_lines : list
        List of linestring to split the line
    """

    list_linestrings = [linestring]

    for p in points:
        # execute split to all lines and store results
        temp_list = [split(l, p) for l in list_linestrings]
        # nest all geometries
        list_linestrings = [lstring for tval in temp_list for lstring in tval.geoms]

    return list_linestrings


def fix_overpassing_lines(lines, buses, distance_crs, tol=1):
    """
    Fix overpassing lines by splitting them at nodes within a given tolerance,
    to include the buses being overpassed.

    Parameters:
    - lines (GeoDataFrame): The lines to be fixed.
    - buses (GeoDataFrame): The buses representing nodes.
    - distance_crs (str): The coordinate reference system (CRS) for distance calculations.
    - tol (float): The tolerance distance in meters for determining if a bus is within a line.
    Returns:
    - lines (GeoDataFrame): The fixed lines.
    - buses (GeoDataFrame): The buses representing nodes.
    """

    lines_to_add = []  # list of lines to be added
    lines_to_split = []  # list of lines that have been split

    lines_epsgmod = lines.to_crs(distance_crs)
    buses_epsgmod = buses.to_crs(distance_crs)

    # set tqdm options for substation ids
    tqdm_kwargs_substation_ids = dict(
        ascii=False,
        unit=" lines",
        total=lines.shape[0],
        desc="Verify lines overpassing nodes ",
    )

    for l in tqdm(lines.index, **tqdm_kwargs_substation_ids):
        # bus indices being within tolerance from the line
        bus_in_tol_epsg = buses_epsgmod[
            buses_epsgmod.geometry.distance(lines_epsgmod.geometry.loc[l]) <= tol
        ]

        # Get boundary points
        endpoint0 = lines_epsgmod.geometry.loc[l].boundary.geoms[0]
        endpoint1 = lines_epsgmod.geometry.loc[l].boundary.geoms[1]

        # Calculate distances
        dist_to_ep0 = bus_in_tol_epsg.geometry.distance(endpoint0)
        dist_to_ep1 = bus_in_tol_epsg.geometry.distance(endpoint1)

        # exclude endings of the lines
        bus_in_tol_epsg = bus_in_tol_epsg[(dist_to_ep0 > tol) | (dist_to_ep1 > tol)]

        if not bus_in_tol_epsg.empty:
            # add index of line to split
            lines_to_split.append(l)

            buses_locs = buses.geometry.loc[bus_in_tol_epsg.index]

            # get new line geometries
            new_geometries = _split_linestring_by_point(lines.geometry[l], buses_locs)
            n_geoms = len(new_geometries)

            # create temporary copies of the line
            df_append = gpd.GeoDataFrame([lines.loc[l]] * n_geoms)
            # update geometries
            df_append["geometry"] = new_geometries
            # update name of the line if there are multiple line segments
            df_append["line_id"] = [
                str(df_append["line_id"].iloc[0])
                + (f"-{letter}" if n_geoms > 1 else "")
                for letter in string.ascii_lowercase[:n_geoms]
            ]

            lines_to_add.append(df_append)

    if not lines_to_add:
        return lines, buses

    df_to_add = gpd.GeoDataFrame(pd.concat(lines_to_add, ignore_index=True))
    df_to_add.set_crs(lines.crs, inplace=True)
    df_to_add.set_index(lines.index[-1] + df_to_add.index, inplace=True)

    # update length
    df_to_add["length"] = df_to_add.to_crs(distance_crs).geometry.length

    # update line endings
    df_to_add = line_endings_to_bus_conversion(df_to_add)

    # remove original lines
    lines.drop(lines_to_split, inplace=True)

    lines = df_to_add if lines.empty else pd.concat([lines, df_to_add])

    lines = gpd.GeoDataFrame(lines.reset_index(drop=True), crs=lines.crs)

    return lines, buses


def _create_station_seeds(buses, buses_polygon, country_shapes, tol = BUS_TOL, distance_crs = DISTANCE_CRS):
    # Drop all buses that have bus_id starting with "way/" or "relation/" prefix
    # They are present in polygon_buffer anyway
    logger.info("Creating aggregated stations based on substation polygons")
    columns = ["bus_id", "country", "voltage", "geometry"]
    filtered_buses = buses[~buses["bus_id"].str.startswith("way/") & ~buses["bus_id"].str.startswith("relation/")]
    

    buses_buffer = gpd.GeoDataFrame(data = filtered_buses[columns], geometry="geometry").set_index("bus_id")
    buses_buffer["geometry"] = buses_buffer["geometry"].to_crs(distance_crs).buffer(tol).to_crs(GEO_CRS)
    buses_buffer["area"] = buses_buffer.to_crs(distance_crs).area

    buses_polygon_buffer = gpd.GeoDataFrame(data = buses_polygon[columns], geometry="geometry").set_index("bus_id")
    buses_polygon_buffer["geometry"] = buses_polygon_buffer["geometry"].to_crs(distance_crs).buffer(tol).to_crs(GEO_CRS)
    buses_polygon_buffer["area"] = buses_polygon_buffer.to_crs(distance_crs).area

    # Find the pole of inaccessibility (PoI) for each polygon, the interior point most distant from a polygon's boundary
    # Garcia-Castellanos & Lombardo 2007
    # doi.org/10.1080/14702540801897809
    buses_polygon_buffer["poi"] = buses_polygon_buffer["geometry"].to_crs(DISTANCE_CRS) \
        .apply(lambda polygon: polylabel(polygon, tolerance = BUS_TOL/2)) \
        .to_crs(GEO_CRS)

    buses_all_buffer = pd.concat([buses_buffer, buses_polygon_buffer])
    
    buses_all_agg = gpd.GeoDataFrame(geometry=[poly for poly in buses_all_buffer.union_all().geoms], crs=GEO_CRS)

    # full spatial join
    buses_all_agg = gpd.sjoin(buses_all_agg, buses_all_buffer, how="left", predicate="intersects").reset_index()
    max_area_idx = buses_all_agg.groupby('index')['area'].idxmax()
    buses_all_agg = buses_all_agg.loc[max_area_idx]
    buses_all_agg = buses_all_agg.drop(columns=["index"])
    buses_all_agg.set_index("bus_id", inplace=True)

    # Find the PoI for all rows where buses_all_agg.poi isna
    poi_missing = buses_all_agg["poi"].isna()
    buses_all_agg.loc[poi_missing, "poi"] = buses_all_agg.loc[poi_missing, "geometry"].to_crs(DISTANCE_CRS) \
        .apply(lambda polygon: polylabel(polygon, tolerance = BUS_TOL/2)) \
        .to_crs(GEO_CRS)

    # Update countries based on the PoI location:
    updated_country_mapping = gpd.sjoin_nearest(
        gpd.GeoDataFrame(buses_all_agg["poi"], geometry="poi").to_crs(DISTANCE_CRS), 
        country_shapes.to_crs(DISTANCE_CRS), 
        how="left"
    )["name"]
    updated_country_mapping.index.name="country"

    buses_all_agg["country"] = updated_country_mapping

    # Rename rows virtual buses that are not actual substations
    buses_to_rename = buses_all_agg.loc[
        (~buses_all_agg.index.str.startswith("way/") & ~buses_all_agg.index.str.startswith("relation/")),
        ["country", "poi"]
    ]

    buses_to_rename['lat'] = buses_to_rename['poi'].apply(lambda p: p.y)
    buses_to_rename['lon'] = buses_to_rename['poi'].apply(lambda p: p.x)

    # Now sort by country, latitude (north to south), and longitude (west to east)
    buses_to_rename = buses_to_rename.sort_values(by=['country', 'lat', 'lon'], ascending=[True, False, True])
    buses_to_rename['bus_id'] = buses_to_rename.groupby('country').cumcount() + 1
    buses_to_rename['bus_id'] = buses_to_rename['country'] + buses_to_rename['bus_id'].astype(str)

    # Dict to rename virtual buses
    dict_rename = buses_to_rename["bus_id"].to_dict()

    # Rename virtual buses in buses_all_agg with dict
    buses_all_agg.rename(index=dict_rename, inplace=True)

    # extract substring before - from index
    buses_all_agg["osm_identifier"] = buses_all_agg.index.str.split("-").str[0]
    buses_all_agg.reset_index(inplace=True)
    # count how often each value in osm_identifier occurs in column
    buses_all_agg["id_occurence"] = buses_all_agg.groupby("osm_identifier")["osm_identifier"].transform("count")

    # For each row, if id_occurence =1 set bus_id = osm_identifier
    buses_all_agg.loc[buses_all_agg["id_occurence"] == 1, "bus_id"] = buses_all_agg.loc[buses_all_agg["id_occurence"] == 1, "osm_identifier"]
    buses_all_agg.set_index("bus_id", inplace=True)
    # Rename index name to station_id
    buses_all_agg.index.name = "station_id"
    buses_all_agg.drop(columns=["voltage", "area", "osm_identifier", "id_occurence"], inplace=True)

    return buses_all_agg


def _create_merge_mapping(lines, buses, buses_polygon):
    logger.info("Creating mapping for merging lines with same electric parameters over virtual buses.")
    lines=lines.copy()
    buses_virtual=buses.copy()

    buses_virtual = buses_virtual[buses_virtual["bus_id"].str.startswith("line-end")]
    # Drop buses_virtual that are inside buses_virtual_polygon
    bool_intersects_buses_virtual_polygon = buses_virtual.intersects(buses_polygon.union_all())
    buses_virtual = buses_virtual[~bool_intersects_buses_virtual_polygon]

    # sjoin with lines_filtered in column "connected_lines", that interesect with buses_virtual
    buses_virtual = gpd.sjoin(
        buses_virtual, 
        lines[["line_id", "geometry", "voltage", "circuits"]], 
        how="left", 
        predicate="touches"
    )
    # Filtering, only keep where voltage_left == voltage_right
    buses_virtual = buses_virtual[buses_virtual["voltage_left"] == buses_virtual["voltage_right"]]
    # Drop voltage_right and rename voltage_left to voltage
    buses_virtual = buses_virtual.drop(columns=["voltage_right"])
    buses_virtual = buses_virtual.rename(columns={"voltage_left": "voltage"})

    # Group by bus_id, voltage, circuits and count the number of connected lines
    buses_to_remove = buses_virtual.groupby(["bus_id", "voltage"]).size().reset_index(name="count")
    buses_to_remove = buses_to_remove[buses_to_remove["count"] == 2]

    buses_to_remove = buses_virtual[buses_virtual["bus_id"].isin(buses_to_remove["bus_id"])]
    buses_to_remove = buses_to_remove.groupby(["bus_id", "circuits", "voltage"]).size().reset_index(name="count")
    # Keep only where count == 2
    buses_to_remove = buses_to_remove[buses_to_remove["count"] == 2]
    buses_to_remove = buses_virtual[buses_virtual["bus_id"].isin(buses_to_remove["bus_id"])]

    # Group by bus_id, voltage, circuits and count the number of connected lines, add column "lines" which contains the list of lines 
    buses_to_remove = buses_to_remove.groupby(["bus_id", "voltage", "circuits"]) \
        .agg({
            "line_id": list,
            "country": list,
        }).reset_index()
    
    unique_lines = pd.Series(chain(*buses_to_remove["line_id"])).unique()
    lines_to_merge = lines.loc[lines["line_id"].isin(unique_lines), ["line_id", "voltage", "circuits", "length", "geometry", "country", "tag_type", "underground"]]
    lines_to_merge_dict = [(node, row.to_dict()) for node, row in lines_to_merge.set_index("line_id").iterrows()]
    
    # Create networkx
    G = nx.Graph()

    # Add nodes from
    # lines_to_merge
    G.add_nodes_from(lines_to_merge_dict)
    
    edges = [
        (
            row["line_id"][0],  # from node
            row["line_id"][1],  # to node
            {
                "bus_id": row["bus_id"],      # shared bus
            }
        )
        for _, row in buses_to_remove.iterrows()
    ]

    # Add all edges at once
    G.add_edges_from(edges)
    
    connected_components = nx.connected_components(G)

    # Create empty 
    merged_lines = pd.DataFrame(columns=["line_id", "circuits", "voltage", "geometry", "country", "tag_type", "underground", "contains_lines", "contains_buses"])

    # Iterate over each connected component
    for i, component in enumerate(connected_components, 1):
        # Create a subgraph for the current component
        subgraph = G.subgraph(component)
        
        # Extract the first node in the component
        first_node = next(iter(component))  # Get the first node (can be arbitrary)
        
        # Extract the attributes for the first node
        circuits = G.nodes[first_node].get('circuits', None)  # Default to None if not present
        voltage = G.nodes[first_node].get('voltage', None)    # Default to None if not present
        
        # Extract the geometries for all nodes in the subgraph
        geometry = [G.nodes[node].get('geometry', None) for node in subgraph.nodes()]
        geometry = linemerge(geometry)  # Merge the geometries

        # list of all countries
        country = ';'.join([G.nodes[node].get('country', '') for node in subgraph.nodes()])
        country = ';'.join(sorted(set(country.split(';'))))

        # Contains lines
        contains_lines = list(subgraph.nodes())
        number_of_lines = int(len(contains_lines))
        # Find the node with the highest "length" parameter
        node_longest = max(subgraph.nodes(), key=lambda node: G.nodes[node].get('length', 0))
        tag_type = G.nodes[node_longest].get('tag_type', None)
        underground = G.nodes[node_longest].get('underground', None)

        # Extract the list of edges (lines) in the subgraph
        contains_buses = list()
        for edge in subgraph.edges():
            contains_buses.append(G.edges[edge].get('bus_id', None))

        subgraph_data = {
            "line_id": ["merged_"+str(node_longest)+"+"+str(number_of_lines-1)],  # line_id
            "circuits": [circuits],
            "voltage": [voltage],
            "geometry": [geometry],
            "country": [country],
            "tag_type": [tag_type],
            "underground": [underground],
            "contains_lines": [contains_lines],
            "contains_buses": [contains_buses],
        }

         # Convert to DataFrame and append to the merged_lines DataFrame
        merged_lines = pd.concat([merged_lines, pd.DataFrame(subgraph_data)], ignore_index=True)
        merged_lines = gpd.GeoDataFrame(merged_lines, geometry="geometry", crs=GEO_CRS)

        # Drop all closed linestrings (circles)
        merged_lines = merged_lines[merged_lines["geometry"].apply(lambda x: not x.is_closed)]
    
    return merged_lines


def _merge_lines_over_virtual_buses(lines, buses, merged_lines_map):
    lines_merged = lines.copy()
    buses_merged = buses.copy()

    lines_to_remove = merged_lines_map["contains_lines"].explode().unique()
    buses_to_remove = merged_lines_map["contains_buses"].explode().unique()

    # Remove lines and buses
    lines_merged = lines_merged[~lines_merged["line_id"].isin(lines_to_remove)]
    lines_merged["contains_lines"] = lines_merged["line_id"].apply(lambda x: [x])
    buses_merged = buses_merged[~buses_merged["bus_id"].isin(buses_to_remove)]
    # Add new lines from merged_lines_map to lines_merged

    ### Update lines to add
    lines_to_add = merged_lines_map.copy().reset_index(drop=True)
    
    # Update columns
    lines_to_add["under_construction"] = False
    lines_to_add["tag_frequency"] = 50
    lines_to_add["dc"] = False
    lines_to_add["bus0"] = None
    lines_to_add["bus1"] = None
    lines_to_add["length"] = lines_to_add["geometry"].to_crs(DISTANCE_CRS).length

    # Add missing geometry columns
    lines_to_add = line_endings_to_bus_conversion(lines_to_add)

    # Reorder
    lines_to_add = lines_to_add[lines_merged.columns]

    # Concatenate
    lines_merged = pd.concat([lines_merged, lines_to_add], ignore_index=True)

    return lines_merged, buses_merged


def build_network(inputs, countries):
    logger.info("Reading input data.")
    buses = gpd.read_file(inputs["substations"])
    buses_polygon = gpd.read_file(inputs["substations_polygon"])
    lines = gpd.read_file(inputs["lines"])
    links = gpd.read_file(inputs["links"])

    lines = line_endings_to_bus_conversion(lines)
    links = line_endings_to_bus_conversion(links)

    logger.info(
        "Fixing lines overpassing nodes: Connecting nodes and splittling lines."
    )
    lines, buses = fix_overpassing_lines(lines, buses, DISTANCE_CRS, tol=1)

    merged_lines_map = _create_merge_mapping(lines, buses, buses_polygon)
    lines, buses = _merge_lines_over_virtual_buses(lines, buses, merged_lines_map)

    # Create station seeds
    stations = _create_station_seeds(buses, buses_polygon, country_shapes, tol = BUS_TOL, distance_crs = DISTANCE_CRS)
    
    ### Debugging - comment out/delete later
    # TODO: Delete
    # map = stations.explore(color="red")
    # map = buses_polygon.explore(m=map, color="yellow", popup=True)
    # map = lines.explore(m=map, color="blue")
    # map = links.explore(m=map, color="purple")
    # map = stations.poi.explore(m=map, color="green", popup=True)
    # map

    lines, links, buses = merge_stations_lines_by_station_id_and_voltage(
        lines, links, buses, countries, stations, DISTANCE_CRS, BUS_TOL
    )

    # Recalculate lengths of lines
    utm = lines.estimate_utm_crs(datum_name="WGS 84")
    lines["length"] = lines.to_crs(utm).length
    links["length"] = links.to_crs(utm).length

    transformers = get_transformers(buses, lines)
    converters = _get_converters(buses, links, DISTANCE_CRS)

    logger.info("Saving outputs")

    ### Convert output to pypsa-eur friendly format
    # Rename "substation" in buses["symbol"] to "Substation"
    buses["symbol"] = buses["symbol"].replace({"substation": "Substation"})

    # Drop unnecessary index column and set respective element ids as index
    lines.set_index("line_id", inplace=True)
    if not links.empty:
        links.set_index("link_id", inplace=True)
    converters.set_index("converter_id", inplace=True)
    transformers.set_index("transformer_id", inplace=True)
    buses.set_index("bus_id", inplace=True)

    # Convert voltages from V to kV
    lines["voltage"] = lines["voltage"] / 1000
    if not links.empty:
        links["voltage"] = links["voltage"] / 1000
    if not converters.empty:
        converters["voltage"] = converters["voltage"] / 1000
    transformers["voltage_bus0"], transformers["voltage_bus1"] = (
        transformers["voltage_bus0"] / 1000,
        transformers["voltage_bus1"] / 1000,
    )
    buses["voltage"] = buses["voltage"] / 1000

    # Convert 'true' and 'false' to 't' and 'f'
    lines = lines.replace({True: "t", False: "f"})
    links = links.replace({True: "t", False: "f"})
    converters = converters.replace({True: "t", False: "f"})
    buses = buses.replace({True: "t", False: "f"})

    # Change column orders
    lines = lines[LINES_COLUMNS]
    if not links.empty:
        links = links[LINKS_COLUMNS]
    else:
        links = pd.DataFrame(columns=["link_id"] + LINKS_COLUMNS)
        links.set_index("link_id", inplace=True)
    transformers = transformers[TRANSFORMERS_COLUMNS]

    return buses, converters, lines, links, transformers


if __name__ == "__main__":
    # Detect running outside of snakemake and mock snakemake for testing
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("build_osm_network")

    configure_logging(snakemake)
    set_scenario_config(snakemake)

    countries = snakemake.params.countries
    country_shapes = gpd.read_file(snakemake.input["country_shapes"]).set_index("name")

    buses, converters, lines, links, transformers = build_network(
        snakemake.input,
        countries,
    )

    # Export to csv for base_network
    buses.to_csv(snakemake.output["substations"], quotechar="'")
    lines.to_csv(snakemake.output["lines"], quotechar="'")
    links.to_csv(snakemake.output["links"], quotechar="'")
    converters.to_csv(snakemake.output["converters"], quotechar="'")
    transformers.to_csv(snakemake.output["transformers"], quotechar="'")

    # Export to GeoJSON for quick validations
    buses.to_file(snakemake.output["substations_geojson"])
    lines.to_file(snakemake.output["lines_geojson"])
    links.to_file(snakemake.output["links_geojson"])
    converters.to_file(snakemake.output["converters_geojson"])
    transformers.to_file(snakemake.output["transformers_geojson"])
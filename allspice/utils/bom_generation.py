# cspell:ignore jsons

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Optional, Union

from ..allspice import AllSpice
from ..apiobject import Ref, Repository
from .list_components import (
    ComponentAttributes,
    SupportedTool,
    infer_project_tool,
    list_components,
)

QUANTITY_COLUMN_NAME = "Quantity"


@dataclass
class ColumnConfig:
    """
    Configuration for a single column in the BOM.
    """

    class SortOrder(Enum):
        """
        Sort order for values in a column.
        """

        ASC = "asc"
        DESC = "desc"

    attributes: list[str] | str
    """
    The attributes of a part to use as the value for this column. If a list,
    each attribute in the list is tried in the order given, and the first one
    found is used. If a string, that attribute is used. If none of the given
    attributes are found, the column will be empty.
    """

    sort: Optional[SortOrder] = None
    """
    Sort order for the values in this column. If multiple columns have sort
    orders, the values will be sorted by the order of the columns in the
    columns mapping.

    The default is no sorting, and the order of the values in the BOM is not
    guaranteed.
    """

    remove_rows_matching: Optional[str] = None
    """
    A regex pattern to match against the values in this column. If a value
    matches this pattern, the **entire** row will be removed from the BOM.
    Filtering is performed after grouping.
    """

    grouped_values_sort: Optional[SortOrder] = None
    """
    If there is a grouping set, and this column is NOT part of the grouping,
    this will determine the sort order of the values in this column. For
    example, if the grouping is by "Reference" and "Value", and the column is
    "Designator", then the designators will be sorted in this order.

    The default is no sorting, and the order of the values in the group is not
    guaranteed.
    """

    grouped_values_separator: str = ", "
    """
    If there is a grouping set, and this column is NOT part of the grouping,
    this will determine the separator between the values in this column. For
    example, if the grouping is by "Reference" and "Value", and the column is
    "Designator", then the designators will be separated by this string.
    """

    grouped_values_allow_duplicates: bool = False
    """
    If there is a grouping set, and this column is NOT part of the grouping,
    this will determine if the values in this column should be deduplicated.
    For example, if the values "C1" and "C1" are found in the "Designator"
    column, and this is set to True, then only one "C1" will be included in
    the BOM. Otherwise, both will be included.
    """

    skip_in_output: bool = False
    """
    If True, this column will be skipped in the output BOM. This can be useful
    for columns that are used for grouping, sorting or filtering, but should
    not be included in the final BOM.
    """


ColumnsMapping = Mapping[str, ColumnConfig | list[str] | str]
"""
Configuration for the columns in the BOM. See `ColumnConfig` for a detailed
description of the configuration for each column. The keys in this dictionary
are the names of the columns in the BOM. The `str` and `list[str]` cases are
shorthand for `ColumnConfig` with the `attributes` field set to the given
value(s) and the other fields set to their defaults.
"""

BomEntry = dict[str, str]
Bom = list[BomEntry]


def generate_bom(
    allspice_client: AllSpice,
    repository: Repository,
    source_file: str,
    columns: ColumnsMapping,
    group_by: Optional[list[str]] = None,
    variant: Optional[str] = None,
    ref: Ref = "main",
    remove_non_bom_components: bool = True,
    design_reuse_repos: list[Repository] = [],
    include_not_fitted: bool = False,
) -> Bom:
    """
    Generate a BOM for a project.

    :param allspice_client: The AllSpice client to use.
    :param repository: The repository to generate the BOM for.
    :param source_file: The path to the source file from the root of the
        repository. The source file must be a PrjPcb file for Altium projects a
        DSN file for OrCAD projects, an SDAX file for System Capture
        projects, or a CPM file for DeHDL projects. For example, if the source file is in the root of the
        repository and is named "Archimajor.PrjPcb", the path would be
        "Archimajor.PrjPcb"; if the source file is in a folder called
        "Schematics" and is named "Beagleplay.dsn", the path would be
        "Schematics/Beagleplay.dsn".
    :param columns: A mapping of the columns in the BOM to the attributes in the
        project. See `ColumnMapping` and `ColumnConfig` for a detailed
        description of the column configuration.

        Note that special attributes are added by this function depending on
        the project tool. For Altium projects, these are "_part_id",
        "_description", "_unique_id" and "_kind", which are the Library
        Reference, Description, Unique ID and Component Type respectively. For
        OrCAD, System Capture, and DeHDL projects, "_name" is added, which is the name
        of the component, and "_reference" and "_logical_reference" may be
        added, which are the name of the component, and the logical reference
        of a multi-part component respectively.

        Two synthetic attributes are always available for Altium projects:
        ``_fitted`` (``"True"`` or ``"False"``) and ``_variation_kind``
        (``"FITTED_MOD_PARAMS"``, ``"NOT_FITTED"``, ``"ALT_COMP"``, or ``""``
        for unmodified components). These can be included as column values to
        report fitted/DNP/alternate status in the BOM output.
    :param group_by: A list of columns to group the BOM by. If this is provided,
        the BOM will be grouped by the values of these columns.
    :param variant: The variant of the project to generate the BOM for. If this
        is provided, the BOM will be generated for the specified variant. If
        this is not provided, or is None, the BOM will be generated without
        considering variants. Variants are supported for all tools where
        AllSpice Hub shows variants.
    :param ref: The ref, i.e. branch, commit or git ref from which to take the
        project files. Defaults to "main".
    :param remove_non_bom_components: If True, components of types that should
        not be included in the BOM will be removed. Defaults to True. Only
        applicable for Altium and DeHDL projects.
    :param include_not_fitted: If True, components marked as not fitted (DNP)
        in the selected variant will be included in the BOM output with
        ``_fitted`` set to ``"False"``. If False (the default), not-fitted
        components are excluded, preserving previous behaviour.
    :return: A list of BOM entries. Each entry is a dictionary where the key is
        a column name and the value is the value for that column.
    """

    allspice_client.logger.info(
        f"Generating BOM for {repository.get_full_name()=} on {ref=} using {columns=}"
    )

    if group_by is not None:
        for group_column in group_by:
            if group_column not in columns:
                raise ValueError(f"Group by column {group_column} not found in selected columns")

    components = list_components(
        allspice_client,
        repository,
        source_file,
        variant,
        ref,
        combine_multi_part=True,
        design_reuse_repos=design_reuse_repos,
    )

    if not include_not_fitted:
        components = [c for c in components if c.get("_fitted", "True") != "False"]

    if remove_non_bom_components:
        project_tool = infer_project_tool(source_file)
        if project_tool == SupportedTool.ALTIUM:
            components = _remove_altium_non_bom_components(components)
        elif project_tool == SupportedTool.DEHDL:
            components = _remove_dehdl_non_bom_components(components)

    columns_mapping = {
        column_name: (
            column_config
            if isinstance(column_config, ColumnConfig)
            else ColumnConfig(attributes=column_config)
        )
        for column_name, column_config in columns.items()
    }

    mapped_components = _map_attributes(components, columns_mapping)
    bom = _group_entries(mapped_components, group_by, columns_mapping)
    bom = _filter_rows(bom, columns_mapping)
    bom = _sort_columns(bom, columns_mapping)
    bom = _skip_columns(bom, columns_mapping)

    return bom


def generate_bom_for_altium(
    allspice_client: AllSpice,
    repository: Repository,
    prjpcb_file: str,
    columns: ColumnsMapping,
    group_by: Optional[list[str]] = None,
    variant: Optional[str] = None,
    ref: Ref = "main",
    remove_non_bom_components: bool = True,
    design_reuse_repos: list[Repository] = [],
    include_not_fitted: bool = False,
) -> Bom:
    """
    Generate a BOM for an Altium project.

    :param allspice_client: The AllSpice client to use.
    :param repository: The repository to generate the BOM for.
    :param prjpcb_file: The path to the PrjPcb project file from the root of the
        repository.
    :param columns: A mapping of the columns in the BOM to the attributes in the
        project. See `ColumnMapping` and `ColumnConfig` for a detailed
        description of the column configuration.

        Note that special attributes are added by this function, namely,
        "_part_id", "_description", "_unique_id" and "_kind", which are the
        Library Reference, Description, Unique ID and Component Type
        respectively.

        Two synthetic attributes are always available: ``_fitted``
        (``"True"`` or ``"False"``) and ``_variation_kind``
        (``"FITTED_MOD_PARAMS"``, ``"NOT_FITTED"``, ``"ALT_COMP"``, or ``""``
        for unmodified components).
    :param group_by: A list of columns to group the BOM by. If this is provided,
        the BOM will be grouped by the values of these columns.
    :param ref: The ref, i.e. branch, commit or git ref from which to take the
        project files. Defaults to "main".
    :param variant: The variant of the project to generate the BOM for. If this
        is provided, the BOM will be generated for the specified variant. If
        this is not provided, or is None, the BOM will be generated for the
        default variant.
    :param remove_non_bom_components: If True, components of types that should
        not be included in the BOM will be removed. Defaults to True.
    :param include_not_fitted: If True, components marked as not fitted (DNP)
        in the selected variant will be included in the BOM output with
        ``_fitted`` set to ``"False"``. If False (the default), not-fitted
        components are excluded, preserving previous behaviour.
    :return: A list of BOM entries. Each entry is a dictionary where the key is
        a column name and the value is the value for that column.
    """

    return generate_bom(
        allspice_client,
        repository,
        prjpcb_file,
        columns,
        group_by,
        variant,
        ref,
        remove_non_bom_components,
        design_reuse_repos=design_reuse_repos,
        include_not_fitted=include_not_fitted,
    )


def generate_bom_for_orcad(
    allspice_client: AllSpice,
    repository: Repository,
    dsn_path: str,
    columns: ColumnsMapping,
    group_by: Optional[list[str]] = None,
    ref: Ref = "main",
) -> Bom:
    """
    Generate a BOM for an OrCAD schematic.

    :param allspice_client: The AllSpice client to use.
    :param repository: The repository to generate the BOM for.
    :param dsn_path: The OrCAD DSN file. This can be a Content object returned
        by the AllSpice API, or a string containing the path to the file in the
        repo.
    :param columns: A mapping of the columns in the BOM to the attributes in the
        project. See `ColumnMapping` and `ColumnConfig` for a detailed
        description of the column configuration.

        Note that special attributes are added by this function, namely, "_name"
        is added, which is the name of the component.
    :param group_by: A list of columns to group the BOM by. If this is provided,
        the BOM will be grouped by the values of these columns.
    :param ref: The ref, i.e. branch, commit or git ref from which to take the
        project files. Defaults to "main".
    :return: A list of BOM entries. Each entry is a dictionary where the key is
        a column name and the value is the value for that column.
    """

    return generate_bom(
        allspice_client,
        repository,
        dsn_path,
        columns,
        group_by,
        ref=ref,
        remove_non_bom_components=False,
    )


def generate_bom_for_system_capture(
    allspice_client: AllSpice,
    repository: Repository,
    sdax_path: str,
    columns: ColumnsMapping,
    group_by: Optional[list[str]] = None,
    variant: Optional[str] = None,
    ref: Ref = "main",
) -> Bom:
    """
    Generate a BOM for a System Capture SDAX schematic.

    :param allspice_client: The AllSpice client to use.
    :param repository: The repository to generate the BOM for.
    :param sdax_path: The System Catpure SDAX schematic file. This can be a
        Content object returned by the AllSpice API, or a string containing the
        path to the file in the repo.
    :param columns: A mapping of the columns in the BOM to the attributes in the
        SDAX schematic. The attributes are tried in order, and the first one
        found is used as the value for that column.
    :param group_by: A list of columns to group the BOM by. If this is provided,
        the BOM will be grouped by the values of these columns.
    :param variant: The variant of the project to generate the BOM for. If this
        is provided, the BOM will be generated for the specified variant. If
        this is not provided, or is None, the BOM will be generated without
        considering variants.
    :param ref: The ref, i.e. branch, commit or git ref from which to take the
        project files. Defaults to "main".
    :return: A list of BOM entries. Each entry is a dictionary where the key is
        a column name and the value is the value for that column.
    """

    return generate_bom(
        allspice_client,
        repository,
        sdax_path,
        columns,
        group_by,
        variant=variant,
        ref=ref,
        remove_non_bom_components=False,
    )


def generate_bom_for_dehdl(
    allspice_client: AllSpice,
    repository: Repository,
    cpm_path: str,
    columns: ColumnsMapping,
    group_by: Optional[list[str]] = None,
    ref: Ref = "main",
    remove_non_bom_components: bool = True,
) -> Bom:
    """
    Generate a BOM for a DeHDL CPM schematic.

    :param allspice_client: The AllSpice client to use.
    :param repository: The repository to generate the BOM for.
    :param cpm_path: The DeHDL CPM schematic file. This can be a
        Content object returned by the AllSpice API, or a string containing the
        path to the file in the repo.
    :param columns: A mapping of the columns in the BOM to the attributes in the
        CPM schematic. The attributes are tried in order, and the first one
        found is used as the value for that column.

        Note that special attributes are added by this function, namely, "_name"
        is added, which is the name of the component.
    :param group_by: A list of columns to group the BOM by. If this is provided,
        the BOM will be grouped by the values of these columns.
    :param ref: The ref, i.e. branch, commit or git ref from which to take the
        project files. Defaults to "main".
    :param remove_non_bom_components: If True, components that should not be
        included in the BOM will be removed. Components with MATERIAL="EMPTY"
        are excluded. Defaults to True.
    :return: A list of BOM entries. Each entry is a dictionary where the key is
        a column name and the value is the value for that column.
    """

    return generate_bom(
        allspice_client,
        repository,
        cpm_path,
        columns,
        group_by,
        variant=None,
        ref=ref,
        remove_non_bom_components=remove_non_bom_components,
    )


def _get_first_matching_key_value(
    alternatives: Union[list[str], str],
    attributes: dict[str, str],
) -> Optional[str]:
    """
    Search for a series of alternative keys in a dictionary, and return the
    value of the first one found.
    """

    if isinstance(alternatives, str):
        alternatives = [alternatives]

    for alternative in alternatives:
        if alternative in attributes:
            value = attributes[alternative]
            if value is not None and value != "":
                return value

    return None


def _map_attributes(
    components: list[ComponentAttributes],
    columns: dict[str, ColumnConfig],
) -> list[BomEntry]:
    """
    Map the attributes of the components to the columns of the BOM using the
    columns mapping. This takes a component as we get it from the JSON and
    returns a dict that can be used as a BOM entry.
    """

    return [
        {
            key: str(_get_first_matching_key_value(value.attributes, component) or "")
            for key, value in columns.items()
        }
        for component in components
    ]


def _group_entries(
    components: list[BomEntry],
    group_by: Optional[list[str]],
    columns_mapping: dict[str, ColumnConfig],
) -> list[BomEntry]:
    """
    Group components based on a list of columns. The order of the columns in the
    list will determine the order of the grouping.

    :returns: A list of rows which can be used as the BOM.
    """

    # If grouping is off, we just add a quantity of 1 to each component and
    # return early.
    if group_by is None or len(group_by) == 0:
        for component in components:
            component[QUANTITY_COLUMN_NAME] = "1"
        return components

    grouped_components = {}
    for component in components:
        key = tuple(component[column] for column in group_by)
        if key in grouped_components:
            grouped_components[key].append(component)
        else:
            grouped_components[key] = [component]

    rows = []

    for components in grouped_components.values():
        row = {}

        # We go through each column in the order they're defined, as that should
        # be the order in the BOM.
        for column, column_config in columns_mapping.items():
            # If we've grouped by this column, the value of the column is the
            # same for all components in the group, so we can just take the
            # value from the first component.
            if column in group_by:
                # The RHS here shouldn't fail as we've validated the group by
                # columns are all in the column selection.
                row[column] = components[0][column]
            # Otherwise, we need to combine the values from all components in
            # the group into one string.
            else:
                if column_config.grouped_values_allow_duplicates:
                    column_values = [str(component[column]) for component in components]
                else:
                    # dict.fromkeys retains the insertion order; set doesn't.
                    column_values = dict.fromkeys(
                        str(component[column]) for component in components
                    ).keys()
                sorted_values = _sort_values(column_values, column_config.grouped_values_sort)
                row[column] = column_config.grouped_values_separator.join(sorted_values)

        row[QUANTITY_COLUMN_NAME] = str(len(components))
        rows.append(row)

    return rows


def _altium_component_excluded_from_bom(component: ComponentAttributes) -> bool:
    """
    Determine if an Altium component should be excluded from the BOM.
    """
    # Multi-page format uses flags to exclude components from the BOM.
    flags = component.get("_flags")
    if isinstance(flags, dict) and flags.get("exclude_from_bom", False):
        return True

    # Legacy format uses component kind to exclude components from the BOM.
    return component.get("_kind") in {"NET_TIE_NO_BOM", "STANDARD_NO_BOM"}


def _remove_altium_non_bom_components(
    components: list[ComponentAttributes],
) -> list[ComponentAttributes]:
    """
    Filter out components of types that should not be included in the BOM.
    """

    return [
        component for component in components if not _altium_component_excluded_from_bom(component)
    ]


def _remove_dehdl_non_bom_components(
    components: list[ComponentAttributes],
) -> list[ComponentAttributes]:
    """
    Filter out DeHDL components that should not be included in the BOM.
    Components with MATERIAL="EMPTY" are excluded from the BOM.
    """

    return [component for component in components if component.get("MATERIAL") != "EMPTY"]


def _sort_values(
    values: Iterable[str],
    sort_order: Optional[ColumnConfig.SortOrder],
) -> Iterable[str]:
    """
    Sort the values in a column based on the sort order.
    """

    if sort_order == ColumnConfig.SortOrder.ASC:
        return sorted(values)
    elif sort_order == ColumnConfig.SortOrder.DESC:
        return sorted(values, reverse=True)
    else:
        return values


def _sort_columns(bom_entries: Bom, columns_config: dict[str, ColumnConfig]) -> Bom:
    """
    Sort the BOM entries based on the sort order of the columns.
    """

    # It is possible (and perhaps faster) to sort the entries using `sorted`
    # and a custom `key` function. However, in multiple attempts to implement a
    # general solution that can handle a variable number of columns and sort
    # orders using `key`, I found it very difficult to read and understand what
    # it was doing. This solution should be slower in the pathological case
    # where *every* column has a sort order, but it is much easier to
    # understand and debug, and should be of comparable speed in the average
    # case where only a few columns have sort orders. Considering that the
    # number of columns is at most in the tens, even the pathological case
    # should be fine.

    # Get sortable columns in reverse order (least to most significant)
    sortable_columns = [
        (name, config) for name, config in reversed(columns_config.items()) if config.sort
    ]

    # Since we're using `sort` which mutates the list in place, we need to copy
    # the list to avoid modifying the original list.
    sorted_bom_entries = bom_entries.copy()

    for column_name, config in sortable_columns:
        reverse = config.sort == ColumnConfig.SortOrder.DESC
        sorted_bom_entries.sort(key=lambda entry: entry.get(column_name, ""), reverse=reverse)

    return sorted_bom_entries


def _filter_rows(bom_entries: Bom, columns_config: dict[str, ColumnConfig]) -> Bom:
    """
    Filter out rows based on the configuration of the columns.
    """

    columns_to_filter = {
        column: config.remove_rows_matching
        for column, config in columns_config.items()
        if config.remove_rows_matching
    }

    if len(columns_to_filter) == 0:
        return bom_entries

    return [
        row
        for row in bom_entries
        if not any(re.search(pattern, row[column]) for column, pattern in columns_to_filter.items())
    ]


def _skip_columns(bom_entries: Bom, columns_config: dict[str, ColumnConfig]) -> Bom:
    """
    Skip columns based on the configuration.
    """

    return [
        {
            column: value
            for column, value in row.items()
            if column not in columns_config or not columns_config[column].skip_in_output
        }
        for row in bom_entries
    ]

# cspell:ignore jsons

import configparser
import dataclasses
import functools
import pathlib
import posixpath
import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from logging import Logger
from typing import Mapping, Optional

from ..allspice import AllSpice
from ..apiobject import Ref, Repository
from ..exceptions import NotFoundException
from .retry_generated import retry_not_yet_generated

PCB_FOOTPRINT_ATTR_NAME = "PCB Footprint"

PART_REFERENCE_ATTR_NAME = "Part Reference"
"""The name of the part reference attribute in OrCAD projects."""

REPETITIONS_REGEX = re.compile(r"Repeat\((\w+),(\d+),(\d+)\)")

DESIGNATOR_COLUMN_NAME = "Designator"
"""The name of the designator attribute in Altium projects."""

LOGICAL_DESIGNATOR = "_logical_designator"

ComponentAttributes = dict[str, str]


class SupportedTool(Enum):
    """
    ECAD tools supported by list_components.
    """

    ALTIUM = "altium"
    ORCAD = "orcad"
    SYSTEM_CAPTURE = "system_capture"
    DEHDL = "dehdl"


class VariationKind(Enum):
    FITTED_MOD_PARAMS = 0
    NOT_FITTED = 1
    ALT_COMP = 2


@dataclass
class AltiumSheetRef:
    """
    An external sheet reference in an Altium Schematic document.
    """

    sheet_name: str
    filename: str
    unique_id: str

    @staticmethod
    def from_legacy_sheet_ref(sheet_ref: dict) -> "AltiumSheetRef":
        return AltiumSheetRef(
            sheet_name=sheet_ref.get("sheet_name", {}).get("name", ""),
            filename=sheet_ref.get("filename", ""),
            unique_id=sheet_ref.get("unique_id", ""),
        )

    @staticmethod
    def from_dict(sheet_ref: dict) -> "AltiumSheetRef":
        return AltiumSheetRef(
            sheet_name=sheet_ref["sheet_name"],
            filename=sheet_ref["filename"],
            unique_id=sheet_ref["id"],
        )


@dataclass
class AltiumChildSheet:
    """
    A child sheet of a schematic in the hierarchy. The fields here are
    Altium-specific.
    """

    class ChildSheetKind(Enum):
        """
        The type of child sheet.
        """

        DOCUMENT = "Document"
        """
        A document in the project.
        """

        DEVICE_SHEET = "DeviceSheet"
        """
        A Device Sheet external to the project.
        """

    kind: ChildSheetKind
    """
    The type of child sheet referenced.
    """

    unique_id: str
    """
    The Unique ID of the SheetRef instance. Note that this may not be unique
    for child sheets, as the same sheetref can be expanded into multiple
    instances via channels.
    """

    name: str
    """
    The name of the child sheet. This can be a path for documents or a name for
    a device sheet.
    """

    channel_name: str
    """
    See https://www.altium.com/documentation/altium-designer/creating-multi-channel-design#the-repeat-keyword
    """


SchdocHierarchy = dict[str, list[AltiumChildSheet]]
"""
Mapping of the name of a sheet to its children.
"""


def list_components(
    allspice_client: AllSpice,
    repository: Repository,
    source_file: str,
    variant: Optional[str] = None,
    ref: Ref = "main",
    combine_multi_part: bool = False,
    design_reuse_repos: list[Repository] = [],
) -> list[ComponentAttributes]:
    """
    Get a list of all components in a schematic.


    Note that special attributes are added by this function depending on
    the project tool. For Altium projects, these are "_part_id",
    "_description", "_unique_id" and "_kind", which are the Library
    Reference, Description, Unique ID and Component Type respectively. For
    OrCAD and System Capture projects, "_name" is added, which is the name
    of the component, and "_reference" and "_logical_reference" may be
    added, which are the name of the component, and the logical reference
    of a multi-part component respectively.

    :param client: An AllSpice client instance.
    :param repository: The repository containing the schematic.
    :param source_file: The path to the schematic file from the repo root. The
        source file must be a PrjPcb file for Altium projects, a DSN file for
        OrCAD projects or an SDAX file for System Capture projects. For
        example, if the schematic is in the folder "Schematics" and the file is
        named "example.DSN", the path would be "Schematics/example.DSN".
    :param variant: The variant to apply to the components. If not None, the
        components will be filtered and modified according to the variant. Only
        applies to Altium projects.
    :param ref: Optional git ref to check. This can be a commit hash, branch
        name, or tag name. Default is "main", i.e. the main branch.
    :param combine_multi_part: If True, multi-part components will be combined
        into a single component.
    :return: A list of all components in the schematic. Each component is a
        dictionary with the keys being the attributes of the component and the
        values being the values of the attributes.
    """

    project_tool = infer_project_tool(source_file)
    match project_tool:
        case SupportedTool.ALTIUM:
            return list_components_for_altium(
                allspice_client,
                repository,
                source_file,
                variant=variant,
                ref=ref,
                combine_multi_part=combine_multi_part,
                design_reuse_repos=design_reuse_repos,
            )
        case SupportedTool.ORCAD:
            if variant:
                raise ValueError("Variant is not supported for OrCAD projects.")

            return list_components_for_orcad(
                allspice_client,
                repository,
                source_file,
                ref=ref,
                combine_multi_part=combine_multi_part,
            )
        case SupportedTool.SYSTEM_CAPTURE:
            return list_components_for_system_capture(
                allspice_client,
                repository,
                source_file,
                variant=variant,
                ref=ref,
                combine_multi_part=combine_multi_part,
            )
        case SupportedTool.DEHDL:
            return list_components_for_dehdl(
                allspice_client,
                repository,
                source_file,
                variant=variant,
                ref=ref,
                combine_multi_part=combine_multi_part,
            )


def list_components_for_altium(
    allspice_client: AllSpice,
    repository: Repository,
    prjpcb_file: str,
    variant: Optional[str] = None,
    ref: Ref = "main",
    combine_multi_part: bool = False,
    design_reuse_repos: list[Repository] = [],
) -> list[ComponentAttributes]:
    """
    Get a list of all components in an Altium project.

    :param client: An AllSpice client instance.
    :param repository: The repository containing the Altium project.
    :param prjpcb_file: The path to the PrjPcb file from the repo root. For
        example, if the PrjPcb file is in the folder "Project" and the file
        is named "example.prjpcb", the path would be "Project/example.prjpcb".
    :param variant: The variant to apply to the components. If not None, the
        components will be filtered and modified according to the variant.
    :param ref: Optional git ref to check. This can be a commit hash, branch
        name, or tag name. Default is "main", i.e. the main branch.
    :param combine_multi_part: If True, multi-part components will be combined
        into a single component.
    :return: A list of all components in the Altium project. Each component is
        a dictionary with the keys being the attributes of the component and the
        values being the values of the attributes. Additionally, `_part_id`,
        `_description`, `_unique_id`, and `_kind` attributes are added to each
        component to store the Library Reference, Description, Unique ID, and
        Component Type respectively.
    """

    allspice_client.logger.info(f"Fetching {prjpcb_file=}")

    # Altium adds the Byte Order Mark to UTF-8 files, so we need to decode the
    # file content with utf-8-sig to remove it. However, some files may contain
    # ISO-8859-1 characters, so we'll try UTF-8 and fall back to ISO-8859-1.
    raw_content = repository.get_raw_file(prjpcb_file, ref=ref)
    try:
        prjpcb_file_contents = raw_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        prjpcb_file_contents = raw_content.decode("iso-8859-1")

    prjpcb_ini = configparser.ConfigParser(interpolation=None)
    prjpcb_ini.read_string(prjpcb_file_contents)

    if variant is not None:
        try:
            variant_details = _extract_variations(variant, prjpcb_ini)
        except ValueError:
            raise ValueError(
                f"Variant {variant} not found in PrjPcb file. Please check the name of the variant."
            )
    else:
        # Ensuring variant_details is always bound, even if it is not used.
        variant_details = None

    project_documents, device_sheets = _extract_schdoc_list_from_prjpcb(prjpcb_ini)
    allspice_client.logger.info("Found %d SchDoc files", len(project_documents))
    allspice_client.logger.info("Found %d Device Sheet files", len(device_sheets))

    if not project_documents:
        raise ValueError("No Project Documents found in the PrjPcb file.")

    try:
        annotations_data = _fetch_and_parse_annotation_file(repository, prjpcb_file, ref)
        allspice_client.logger.info("Found annotations file, %d entries", len(annotations_data))
    except Exception as e:
        if device_sheets:
            allspice_client.logger.warning("Failed to fetch annotations file: %s", e)
            allspice_client.logger.warning("Component designators may not be correct.")
        annotations_data = {}

    # Mapping of schdoc file paths from the project file to their JSON
    schdoc_jsons: dict[str, dict] = {}
    # Mapping of device sheet *names* to their JSON.
    device_sheet_jsons: dict[str, dict] = {}

    # When working with Device Sheets, there are three different paths to keep
    # in mind:
    #
    # 1. The path as given in the PrjPcb file: Used to find a possible match in
    #    the current repo, e.g. if it is a monorepo. If not found, we get the
    #    basename and use that to match in the design reuse repos, yielding (2)
    #    after which this is no longer used.
    # 2. The path of the file in the design reuse repository: Used for fetching
    #    the JSON, after which this is no longer used.
    # 3. The name stored in the `filename` property of the SheetRef: This is the
    #    stem of the path. We need to match a SheetRef to the device sheet it is
    #    referring to using this, which is why `device_sheet_jsons` uses these
    #    as the keys.

    for schdoc_file in project_documents:
        schdoc_path_from_repo_root = _resolve_prjpcb_relative_path(schdoc_file, prjpcb_file)

        schdoc_json = _normalize_schdoc_json(
            retry_not_yet_generated(
                repository.get_generated_json,
                schdoc_path_from_repo_root,
                ref,
                params={"use_new_schdoc_renderer": "true"},
            )
        )
        schdoc_jsons[schdoc_path_from_repo_root] = schdoc_json

    for device_sheet in device_sheets:
        device_sheet_repo, device_sheet_path = _find_device_sheet(
            device_sheet,
            repository,
            prjpcb_file,
            design_reuse_repos,
            allspice_client.logger,
        )
        device_sheet_json = _normalize_schdoc_json(
            retry_not_yet_generated(
                device_sheet_repo.get_generated_json,
                device_sheet_path.as_posix(),
                # Note the default branch here - we can't assume the same ref is
                # available.
                device_sheet_repo.default_branch,
                params={"use_new_schdoc_renderer": "true"},
            )
        )
        device_sheet_jsons[device_sheet_path.stem] = device_sheet_json

    found_legacy = any(_is_legacy_schdoc_json(schdoc_json) for schdoc_json in schdoc_jsons.values())
    found_multi_page = any(
        not _is_legacy_schdoc_json(schdoc_json) for schdoc_json in schdoc_jsons.values()
    )

    if found_legacy and found_multi_page:
        raise ValueError(
            "Schematic sheets must all use the same JSON format: "
            "cannot mix legacy and multi-page schdoc JSONs."
        )
    use_legacy_processing = found_legacy

    independent_sheets, hierarchy = _build_schdoc_hierarchy(
        schdoc_jsons, device_sheet_jsons, use_legacy_processing
    )

    unique_ids_mapping = _create_unique_ids_mapping(prjpcb_ini)
    if device_sheets:
        hierarchy = _correct_device_sheet_reference_unique_ids(
            hierarchy,
            prjpcb_ini,
            allspice_client.logger,
        )

    allspice_client.logger.debug("Independent sheets: %s", independent_sheets)
    allspice_client.logger.debug("Hierarchy: %s", hierarchy)

    # Now we can build a combined mapping of documents and device sheets:
    sheets_to_components = {}
    for schdoc_file, schdoc_json in schdoc_jsons.items():
        sheets_to_components[schdoc_file] = _extract_components_from_schdoc_json(
            schdoc_json, use_legacy_processing
        )
    for device_sheet_name, device_sheet_json in device_sheet_jsons.items():
        sheets_to_components[device_sheet_name] = _extract_components_from_schdoc_json(
            device_sheet_json, use_legacy_processing
        )

    components = []

    for independent_sheet in independent_sheets:
        components.extend(
            _extract_components_altium(
                independent_sheet,
                sheets_to_components,
                hierarchy,
                parent_sheet_id="",
                current_sheet=None,
                use_legacy_processing=use_legacy_processing,
            )
        )

    # At this stage, we have components where the designators are not final.

    if annotations_data:
        components = _apply_annotation_file(
            components,
            annotations_data,
            unique_ids_mapping,
            allspice_client.logger,
        )
    else:
        components = _compute_repetitions(components)

    if combine_multi_part:
        # Multi part components must be combined *after* we've processed
        # repetitions and before we apply variations. This is because each
        # repetition of a multi-part component is treated as a separate
        # component, and they can be present across sheets. We need to combine
        # them into a single component before applying variations, as Altium
        # variations will apply to the combined component.
        components = _combine_multi_part_components_for_altium(components)

    # Remove temporary fields used for prcessing logic
    for component in components:
        component.pop("_attributes", None)

    if variant is not None:
        if variant_details is None:
            # This should never happen, but mypy doesn't know that.
            raise ValueError(f"Variant {variant} not found in PrjPcb file.")

        components = _apply_variations(components, variant_details, allspice_client.logger)
    else:
        for component in components:
            component["_fitted"] = "True"
            component["_variation_kind"] = ""

    return _filter_blank_components(components, allspice_client.logger)


def list_components_for_orcad(
    allspice_client: AllSpice,
    repository: Repository,
    dsn_path: str,
    variant: Optional[str] = None,
    ref: Ref = "main",
    combine_multi_part: bool = False,
) -> list[ComponentAttributes]:
    """
    Get a list of all components in an OrCAD DSN schematic.

    :param client: An AllSpice client instance.
    :param repository: The repository containing the OrCAD schematic.
    :param dsn_path: The path to the OrCAD DSN file from the repo root. For
        example, if the schematic is in the folder "Schematics" and the file
        is named "example.dsn", the path would be "Schematics/example.dsn".
    :param ref: Optional git ref to check. This can be a commit hash, branch
        name, or tag name. Default is "main", i.e. the main branch.
    :param variant: The variant to apply to the components. If not None, the
        components will be filtered and modified according to the variant.
        Variants are supported for all tools where AllSpice Hub shows variants.
    :param combine_multi_part: If True, multi-part components will be combined
        into a single component.
    :return: A list of all components in the OrCAD schematic. Each component is
        a dictionary with the keys being the attributes of the component and the
        values being the values of the attributes. A `_name` attribute is added
        to each component to store the name of the component.
    """

    components = _list_components_multi_page_schematic(
        allspice_client, repository, dsn_path, variant, ref
    )

    if combine_multi_part:
        components = _combine_multi_part_components_for_orcad(components)

    return components


def list_components_for_system_capture(
    allspice_client: AllSpice,
    repository: Repository,
    sdax_path: str,
    variant: Optional[str] = None,
    ref: Ref = "main",
    combine_multi_part: bool = False,
) -> list[ComponentAttributes]:
    """
    Get a list of all components in a System Capture SDAX schematic.

    :param client: An AllSpice client instance.
    :param repository: The repository containing the System Capture schematic.
    :param sdax_path: The path to the System Capture SDAX file from the repo
        root. For example, if the schematic is in the folder "Schematics" and
        the file is named "example.sdax", the path would be
        "Schematics/example.sdax".
    :param variant: The variant to apply to the components. If not None, the
        components will be filtered and modified according to the variant.
        Variants are supported for all tools where AllSpice Hub shows variants.
    :param ref: Optional git ref to check. This can be a commit hash, branch
        name, or tag name. Default is "main", i.e. the main branch.
    :param combine_multi_part: If True, multi-part components will be combined
        into a single component.
    """

    components = _list_components_multi_page_schematic(
        allspice_client, repository, sdax_path, variant, ref
    )

    if combine_multi_part:
        components = _combine_multi_part_components_for_system_capture(components)

    return components


def list_components_for_dehdl(
    allspice_client: AllSpice,
    repository: Repository,
    cpm_path: str,
    variant: Optional[str] = None,
    ref: Ref = "main",
    combine_multi_part: bool = False,
) -> list[ComponentAttributes]:
    """
    Get a list of all components in a DeHDL CPM schematic.

    :param client: An AllSpice client instance.
    :param repository: The repository containing the DeHDL schematic.
    :param cpm_path: The path to the DeHDL CPM file from the repo
        root. For example, if the schematic is in the folder "Schematics" and
        the file is named "example.cpm", the path would be
        "Schematics/example.cpm".
    :param variant: The variant to apply to the components. If not None, the
        components will be filtered and modified according to the variant.
        Variants are supported for all tools where AllSpice Hub shows variants.
    :param ref: Optional git ref to check. This can be a commit hash, branch
        name, or tag name. Default is "main", i.e. the main branch.
    :param combine_multi_part: If True, multi-part components will be combined
        into a single component. This prevents double-counting components that
        appear on multiple schematic pages but represent the same physical component.
    """

    components = _list_components_multi_page_schematic(
        allspice_client, repository, cpm_path, variant, ref
    )

    if combine_multi_part:
        components = _combine_multi_part_components_for_dehdl(components)

    return components


def infer_project_tool(source_file: str) -> SupportedTool:
    """
    Infer the ECAD tool used in a project from the file extension.
    """

    if source_file.lower().endswith(".prjpcb"):
        return SupportedTool.ALTIUM
    elif source_file.lower().endswith(".dsn"):
        return SupportedTool.ORCAD
    elif source_file.lower().endswith(".sdax"):
        return SupportedTool.SYSTEM_CAPTURE
    elif source_file.lower().endswith(".cpm"):
        return SupportedTool.DEHDL
    else:
        raise ValueError("""
The source file for generate_bom must be:

- A PrjPcb file for Altium projects; or
- A DSN file for OrCAD projects; or
- An SDAX file for System Capture projects; or
- A CPM file for DeHDL projects.
        """)


def _is_legacy_schdoc_json(schdoc_json: dict) -> bool:
    return "html_id" in schdoc_json


def _normalize_schdoc_json(schdoc_json: dict) -> dict:
    if _is_legacy_schdoc_json(schdoc_json):
        return schdoc_json
    else:
        pages = schdoc_json.get("pages", [])
        if len(pages) == 0:
            raise ValueError("Multi-page schdoc JSON has no pages.")
        return pages[0]


def _extract_components_from_schdoc_json(
    schdoc_json: dict, use_legacy_processing: bool
) -> list[dict]:
    """
    Extract components from a schdoc JSON.
    """
    if use_legacy_processing:
        return [
            value
            for value in schdoc_json.values()
            if isinstance(value, dict) and value.get("type") == "Component"
        ]
    else:
        return [
            value for value in schdoc_json.get("components", {}).values() if isinstance(value, dict)
        ]


def _list_components_multi_page_schematic(
    allspice_client: AllSpice,
    repository: Repository,
    schematic_path: str,
    variant: Optional[str],
    ref: Ref,
) -> list[dict[str, str]]:
    """
    Internal function for getting all components from a multi-page schematic.

    This pattern is followed by OrCAD and System Capture, and potentially other
    formats in the future.
    """

    allspice_client.logger.debug(
        f"Listing components in {schematic_path=} from {repository.get_full_name()} on {ref=}"
    )

    variant_id = ""

    # verify that the provided variant exists and convert to an id
    if variant is not None:
        prj_data = retry_not_yet_generated(
            repository.get_generated_projectdata, schematic_path, ref
        )
        if "variants" in prj_data:
            for id, name in prj_data["variants"].items():
                if name == variant:
                    variant_id = id

        if variant_id == "":
            raise NotFoundException("Variant %s does not exist in design." % variant)

    # Get the generated JSON for the schematic.
    schematic_json = retry_not_yet_generated(repository.get_generated_json, schematic_path, ref)
    pages = schematic_json["pages"]
    components = []

    for page in pages:
        for component in page["components"].values():
            if (
                variant is not None
                and "variants" in component
                and variant_id in component["variants"]
            ):
                var_state = component["variants"][variant_id]

                if var_state is not None:
                    # Component is overridden in this variant (ALT_COMP or FITTED_MOD_PARAMS).
                    # The multi-page JSON doesn't expose the kind integer, so we can't
                    # distinguish the two; both are fitted. _variation_kind is set to
                    # "ALT_COMP" as the best available approximation.
                    component_attributes = _component_attributes_multi_page(var_state)
                    component_attributes["_fitted"] = "True"
                    component_attributes["_variation_kind"] = "ALT_COMP"
                else:
                    # not fitted in this variant — include with fitted=False
                    component_attributes = _component_attributes_multi_page(component)
                    component_attributes["_fitted"] = "False"
                    component_attributes["_variation_kind"] = "NOT_FITTED"
                components.append(component_attributes)

            else:
                component_attributes = _component_attributes_multi_page(component)
                component_attributes["_fitted"] = "True"
                component_attributes["_variation_kind"] = ""
                components.append(component_attributes)

    return _filter_blank_components(components, allspice_client.logger)


@functools.cache
def _fetch_all_files_in_repo(repo: Repository) -> list[str]:
    """
    Fetch a list of all files in a repository at the default branch.

    :param repo: The repository.
    :returns: A list of all file paths in the repo.
    """

    tree = repo.get_tree(recursive=True)

    return [file.path for file in tree]


def _extract_schdoc_list_from_prjpcb(
    prjpcb_ini: configparser.ConfigParser,
) -> tuple[set[str], set[str]]:
    """
    Extract sets of all schematic files used in this project.

    :param prjpcb_ini: The contents of the PrjPcb file as a ConfigParser.
    :returns: two sets. The first set is of relative paths from the project
        file to all the Schematic Documents in this project, and the second is
        of relative paths to device sheets from the project file.
    """

    schdoc_files = set()
    device_sheets = set()

    for section in prjpcb_ini.sections():
        if section.casefold().startswith("document"):
            document_path = prjpcb_ini[section].get("DocumentPath")
            if document_path and document_path.casefold().endswith(".schdoc"):
                schdoc_files.add(document_path)
        elif section.casefold().startswith("devicesheet"):
            document_path = prjpcb_ini[section].get("DocumentPath")
            if document_path and document_path.casefold().endswith(".schdoc"):
                device_sheets.add(document_path)

    return (schdoc_files, device_sheets)


def _resolve_prjpcb_relative_path(schdoc_path: str, prjpcb_path: str) -> str:
    """
    Convert a relative path to the SchDoc file to an absolute path from the git
    root based on the path to the PrjPcb file.
    """

    # The paths in the PrjPcb file are Windows paths, and ASH will store the
    # paths as Posix paths. We need to resolve the SchDoc path relative to the
    # PrjPcb path (which is a Posix Path, since it is from ASH), and then
    # convert the result into a posix path as a string for use in ASH.
    schdoc = pathlib.PureWindowsPath(schdoc_path)
    prjpcb = pathlib.PurePosixPath(prjpcb_path)
    return posixpath.normpath((prjpcb.parent / schdoc).as_posix())


def _build_schdoc_hierarchy(
    schematic_document_jsons: Mapping[str, dict],
    device_sheet_jsons: Mapping[str, dict],
    use_legacy_processing: bool,
) -> tuple[set[str], SchdocHierarchy]:
    """
    Build a hierarchy of sheets from a mapping of sheet names to the references
    of their children.

    :param document_jsons: A mapping of document sheet paths from project root
        to their JSON.
    :param device_sheet_jsons: A mapping of device sheet *names* to their JSON.
    :returns: The output of this function is a tuple of two values:

    1. A set of "independent" sheets, which can be taken to be roots of the
       hierarchy.
    2. A mapping of each sheet that has children to a list of tuples, where
       each tuple is a child sheet and the number of repetitions of that child
       sheet in the parent sheet. If a sheet has no children and is not a child
       of any other sheet, it will be mapped to an empty list.
    """

    hierarchy: SchdocHierarchy = {}

    if use_legacy_processing:
        schematic_document_refs = {
            filename: [
                AltiumSheetRef.from_legacy_sheet_ref(value)
                for value in schdoc_json.values()
                if isinstance(value, dict) and value.get("type") == "SheetRef"
            ]
            for filename, schdoc_json in schematic_document_jsons.items()
        }

        device_sheet_refs = {
            filename: [
                AltiumSheetRef.from_legacy_sheet_ref(value)
                for value in schdoc_json.values()
                if isinstance(value, dict) and value.get("type") == "SheetRef"
            ]
            for filename, schdoc_json in device_sheet_jsons.items()
        }
    else:
        schematic_document_refs = {
            filename: [
                AltiumSheetRef.from_dict(value)
                for value in schdoc_json.get("sheet_refs", {}).values()
            ]
            for filename, schdoc_json in schematic_document_jsons.items()
        }

        device_sheet_refs = {
            filename: [
                AltiumSheetRef.from_dict(value)
                for value in schdoc_json.get("sheet_refs", {}).values()
            ]
            for filename, schdoc_json in device_sheet_jsons.items()
        }

    # We start by assuming all the document sheets are independent. Device
    # sheets cannot be independent, as they must be referred to by a document
    # sheet or another device sheet.
    independent_sheets = set(schematic_document_refs.keys())
    # This is what we'll use to compare with the sheet names in repetitions.
    schematic_document_names_downcased = {sheet.casefold(): sheet for sheet in independent_sheets}
    device_sheet_names_downcased = {sheet.casefold(): sheet for sheet in device_sheet_refs.keys()}

    # First, we'll build the hierarchy for device sheets, since they cannot
    # point to document sheets.
    for device_sheet_name, device_sheet_refs in device_sheet_refs.items():
        if not device_sheet_refs:
            continue

        repetitions = _extract_repetitions(device_sheet_refs)
        for child_sheet in repetitions:
            # child_sheet should just be a filename
            child_name = device_sheet_names_downcased[child_sheet.name.casefold()]
            # We have to replace the name in the sheet ref with the name in the
            # project file we expect
            child_sheet = dataclasses.replace(
                child_sheet,
                name=child_name,
                kind=AltiumChildSheet.ChildSheetKind.DEVICE_SHEET,
            )
            hierarchy.setdefault(device_sheet_name, []).append(child_sheet)

    # Now for the document sheets, which can point to both other document
    # sheets and device sheets
    for schematic_document_sheet, refs in schematic_document_refs.items():
        if not refs or len(refs) == 0:
            continue

        repetitions = _extract_repetitions(refs)

        for child_sheet in repetitions:
            child_sheet_name_downcased = child_sheet.name.casefold()

            # Search for path in document names that matches the end of the
            # child sheet path. This is necessary due to managed sheets
            # in Altium being stored in arbitrary subdirectories.
            matching_path = next(
                (
                    p
                    for p in schematic_document_names_downcased.keys()
                    if p == child_sheet_name_downcased
                    or p.endswith("/" + child_sheet_name_downcased)
                ),
                None,
            )
            if matching_path is not None:
                child_name = schematic_document_names_downcased[matching_path]
            else:
                # Note the `child_sheet` below - we use the bare text without
                # any path resolution for device sheets.
                child_name = device_sheet_names_downcased[child_sheet_name_downcased]
                # Now we know that this child sheet is a Device Sheet, we can
                # replace the kind.
                child_sheet = dataclasses.replace(
                    child_sheet,
                    kind=AltiumChildSheet.ChildSheetKind.DEVICE_SHEET,
                )

            # We have to replace the name in the sheet ref with the name in the project file we expect
            child_sheet = dataclasses.replace(child_sheet, name=child_name)
            hierarchy.setdefault(schematic_document_sheet, []).append(child_sheet)
            independent_sheets.discard(child_name)

    return (independent_sheets, hierarchy)


def _extract_repetitions(sheet_refs: list[AltiumSheetRef]) -> list[AltiumChildSheet]:
    """
    Takes a list of sheet references and returns all child sheets, which include
    the repetition count for each child sheet.
    """

    repetitions: list[AltiumChildSheet] = []

    for sheet_ref in sheet_refs:
        sheet_name = sheet_ref.sheet_name
        sheet_file_name = sheet_ref.filename
        if sheet_file_name is None:
            raise ValueError(
                "Found a sheet reference with no filename attribute. Please check sheet references in this "
                "project for an empty file path."
            )

        if match := REPETITIONS_REGEX.search(sheet_name):
            channel_identifier = match.group(1)
            start = int(match.group(2))
            end = int(match.group(3))

            for channel_index in range(start, end + 1):
                repetitions.append(
                    AltiumChildSheet(
                        # At this stage, we assume everything is a document, as
                        # we can't know if it is a device sheet or not.
                        kind=AltiumChildSheet.ChildSheetKind.DOCUMENT,
                        name=sheet_file_name,
                        # The UniqueID of this specific instance has to include
                        # the channel index before it.
                        unique_id=f"{channel_index}{sheet_ref.unique_id}",
                        channel_name=f"{channel_identifier}{channel_index}",
                    )
                )
        else:
            repetitions.append(
                AltiumChildSheet(
                    # At this stage, we assume everything is a document, as
                    # we can't know if it is a device sheet or not.
                    kind=AltiumChildSheet.ChildSheetKind.DOCUMENT,
                    name=sheet_file_name,
                    unique_id=sheet_ref.unique_id,
                    channel_name=sheet_name,
                )
            )

    return repetitions


def _component_attributes_altium_legacy(component: dict) -> ComponentAttributes:
    """
    Extract the attributes of a component into a dict.

    This also adds some top-level properties of the component that are not
    attributes into the dict.
    """

    attributes = {}

    for key, value in component["attributes"].items():
        attributes[key] = value["text"]

    # The designator attribute has a `value` key which contains the unchanged
    # designator from the schematic file. This is useful when combining multi-
    # part components.
    attributes[LOGICAL_DESIGNATOR] = component["attributes"][DESIGNATOR_COLUMN_NAME]["value"]

    if "part_id" in component:
        attributes["_part_id"] = component["part_id"]
    if "name" in component:
        attributes["_name"] = component["name"]
    if "description" in component:
        attributes["_description"] = component["description"]
    if "unique_id" in component:
        attributes["_unique_id"] = component["unique_id"]
    if "kind" in component:
        attributes["_kind"] = component["kind"]
    if "part_count" in component:
        attributes["_part_count"] = component["part_count"]
        attributes["_current_part_id"] = component["current_part_id"]
    if "pins" in component:
        attributes["_pins"] = component["pins"]

    return attributes


def _component_attributes_altium_multi_page(component: dict) -> ComponentAttributes:
    """
    Extract attributes of a component from a multi-page Altium document into a dict.
    """
    attributes = {}
    attributes["_name"] = component["name"]
    if "reference" in component:
        attributes["_reference"] = component.get("reference")
    if "logical_reference" in component:
        attributes["_logical_reference"] = component.get("logical_reference")
    if "pins" in component:
        attributes["_pins"] = list(component["pins"].values())

    for attribute in component["attributes"].values():
        value = attribute["value"]
        # If value is a formula, attempt to use display_text instead
        if isinstance(value, str) and value.startswith("="):
            attributes[attribute["name"]] = attribute.get("display_text", value)
        else:
            attributes[attribute["name"]] = value

    attributes["_part_id"] = component["name"]
    if "flags" in component:
        attributes["_flags"] = component["flags"]
    if "description" in component:
        attributes["_description"] = component["description"]
    attributes["_unique_id"] = component["id"]

    # Multi-part components have a _logical_reference attribute, which is the base designator.
    if "_logical_reference" in attributes and attributes["_logical_reference"] is not None:
        attributes[LOGICAL_DESIGNATOR] = attributes["_logical_reference"]
        # Store a copy of raw attributes for merging multi-part components later.
        attributes["_attributes"] = component["attributes"]
    else:
        attributes[LOGICAL_DESIGNATOR] = component["attributes"][DESIGNATOR_COLUMN_NAME]["value"]

    return attributes


def _component_attributes_multi_page(component: dict) -> ComponentAttributes:
    """
    Extract attributes of components from a multi-page document into a dict.

    This also adds some of the top-level properties of the component that
    are not attributes into the dict.
    """

    component_attributes = {}

    component_attributes["_name"] = component["name"]
    if "reference" in component:
        component_attributes["_reference"] = component.get("reference")
    if "logical_reference" in component:
        component_attributes["_logical_reference"] = component.get("logical_reference")
    if "pins" in component:
        # Multi-sheet documents have pins as an object of ids to pin objects.
        # The id is also inside the pin object, so we can flatten this into an
        # array, which is also compatible with how Altium outputs them.
        component_attributes["_pins"] = list(component["pins"].values())

    for attribute in component["attributes"].values():
        component_attributes[attribute["name"]] = attribute["value"]

    return component_attributes


def _letters_for_repetition(rep: int) -> str:
    """
    Generate the letter suffix for a repetition number. If the repetition is
    more than 26, the suffix will be a combination of letters.
    """

    first = ord("A")
    suffix = ""

    while rep > 0:
        u = (rep - 1) % 26
        letter = chr(u + first)
        suffix = letter + suffix
        rep = (rep - u) // 26

    return suffix


def _extract_components_altium(
    sheet_name: str,
    sheets_to_entries: dict[str, list[dict]],
    hierarchy: SchdocHierarchy,
    parent_sheet_id: str,
    current_sheet: AltiumChildSheet | None = None,
    use_legacy_processing: bool = False,
) -> list[ComponentAttributes]:
    """
    Extract the components from a sheet in an Altium project.

    :param sheet_name: The name of the sheet to extract components from. This
        is required because independent sheets do not have an AltiumChildSheet
        instance, and we need the name to look up the components.
    :param sheets_to_entries: A mapping of sheet names to the entries in the
        JSON for that sheet.
    :param hierarchy: The hierarchy of the sheets in the project.
    :param parent_sheet_id: The Unique ID of the parent sheet in the hierarchy.
        For independent sheets, this should be empty.
    :param current_sheet: The AltiumChildSheet instance for the current sheet.
        If this is an independent sheet, this should be None.
    :returns: A list of components in the sheet. The designators of the
        components here are not final and should be patched, either with the
        Annotation file or through the repetitions logic.
    """

    components = []

    if current_sheet is not None:
        current_sheet_id = f"{parent_sheet_id}\\{current_sheet.unique_id}"
    else:
        current_sheet_id = parent_sheet_id

    if sheet_name not in sheets_to_entries:
        return components

    for entry in sheets_to_entries[sheet_name]:
        if use_legacy_processing:
            component = _component_attributes_altium_legacy(entry)
        else:
            component = _component_attributes_altium_multi_page(entry)
        component_unique_id = f"{current_sheet_id}\\{component['_unique_id']}"
        component["_unique_id"] = component_unique_id
        if current_sheet is not None:
            component["_channel_name"] = current_sheet.channel_name

        components.append(component)

    if sheet_name not in hierarchy:
        return components

    for child_sheet in hierarchy[sheet_name]:
        child_components = _extract_components_altium(
            child_sheet.name,
            sheets_to_entries,
            hierarchy,
            current_sheet_id,
            child_sheet,
            use_legacy_processing,
        )

        components.extend(child_components)

    return components


def _combine_multi_part_components_for_altium(
    components: list[dict[str, str]],
) -> list[dict[str, str]]:
    """
    Combine multi-part Altium components into a single component.

    In the legacy format, multi-part components have `_part_count` and
    `_current_part_id` attributes. In the new multi-page format, multi-part
    components have a `_logical_reference` attribute (the base designator).
    In both cases, the `_logical_designator` attribute ties together the
    different parts of the component.
    """

    combined_components = []
    multi_part_components_by_designator = {}

    for component in components:
        # Legacy detection: _part_count + _current_part_id
        # New format detection: _logical_reference present
        is_multi_part = (
            "_part_count" in component and "_current_part_id" in component
        ) or "_logical_reference" in component
        if is_multi_part:
            designator = component[LOGICAL_DESIGNATOR]
            multi_part_components_by_designator.setdefault(designator, []).append(component)
        else:
            combined_components.append(component)

    for designator, multi_part_components in multi_part_components_by_designator.items():
        # Sort by _reference with a fallback to _current_part_id to make sure we use a consistent first part, which is the "anchor" part.
        sorted_multi_part_components = sorted(
            multi_part_components,
            key=lambda x: x.get("_reference", x.get("_current_part_id", "")),
        )
        # Merge attributes from all parts, taking the first non-empty value
        # for each key if it's not an attribute belonging to just that part.
        combined_component = {}
        for part in sorted_multi_part_components:
            for key, value in part.items():
                if key not in combined_component or not combined_component[key]:
                    # The chain down to 'symbol' might be undefined at any level, which is equivalent to being
                    # set to 'AllSymbols'. We're only trying to detect the uncommon case of 'symbol' being set and having a
                    # value other than 'AllSymbols'.
                    is_single_part_attribute = (
                        part.get("_attributes", {}).get(key, {"symbol": "AllSymbols"}).get("symbol")
                        != "AllSymbols"
                    )
                    if not is_single_part_attribute:
                        combined_component[key] = value
        combined_component[DESIGNATOR_COLUMN_NAME] = designator
        # The combined component shouldn't have the current part id, as it is
        # not any of the parts.
        combined_component.pop("_current_part_id", None)
        combined_components.append(combined_component)

    return combined_components


def _combine_multi_part_components_for_orcad(
    components: list[dict[str, str]],
) -> list[dict[str, str]]:
    """
    Combine multi-part OrCAD components into a single component.

    Multi-part OrCAD components can be distinguished by the "logical_reference"
    attribute, which ties together the different parts of the component.
    """

    combined_components = []
    multi_part_components_by_designator = {}

    for component in components:
        if "_logical_reference" in component:
            designator = component["_logical_reference"]
            multi_part_components_by_designator.setdefault(designator, []).append(component)
        else:
            combined_components.append(component)

    for designator, multi_part_components in multi_part_components_by_designator.items():
        combined_component = multi_part_components[0].copy()
        combined_component[PART_REFERENCE_ATTR_NAME] = designator
        combined_component["_reference"] = designator
        combined_components.append(combined_component)

    return combined_components


def _combine_multi_part_components_for_system_capture(
    components: list[dict[str, str]],
) -> list[dict[str, str]]:
    """
    Combine multi-part System Capture components into a single component.

    Multi-part System Capture components share the same LOCATION (reference
    designator) but have different SEC (section) values. For multi-part
    components, only the component with SEC=1 is kept as a representation the
    complete component. Single-part components are always kept regardless of
    their SEC value.
    """

    location_counts = Counter(comp["LOCATION"] for comp in components if comp.get("LOCATION"))

    combined_components = []
    for component in components:
        location = component.get("LOCATION", "")
        if not location:
            combined_components.append(component)
            continue

        if location_counts[location] == 1:
            combined_components.append(component)
        elif component.get("SEC") == "1":
            combined_components.append(component)

    return combined_components


def _combine_multi_part_components_for_dehdl(
    components: list[dict[str, str]],
) -> list[dict[str, str]]:
    """
    Combine multi-part components for DeHDL projects. Note: this is a temporary
    solution. We'll add a more robust mechanism for handling multipart components
    to the design JSON.

    In DeHDL, multipart components share the same LOCATION (reference designator)
    but may appear on different pages. This function combines them into single
    components with quantity 1 to prevent double-counting in BOM generation.

    Components with the same LOCATION are treated as a single physical component.
    """
    seen_locations = set()
    combined_components = []

    for component in components:
        location = component.get("LOCATION", "")
        if location and location not in seen_locations:
            seen_locations.add(location)
            combined_components.append(component)

    return combined_components


def _extract_variations(
    variant: str,
    prjpcb_ini: configparser.ConfigParser,
) -> configparser.SectionProxy:
    """
    Extract the details of a variant from a PrjPcb file.
    """

    available_variants = set()

    for section in prjpcb_ini.sections():
        if section.startswith("ProjectVariant"):
            if prjpcb_ini[section].get("Description") == variant:
                return prjpcb_ini[section]
            else:
                available_variants.add(prjpcb_ini[section].get("Description"))
    raise ValueError(
        f"Variant {variant} not found in PrjPcb file.\n"
        f"Available variants: {', '.join(available_variants)}"
    )


def _apply_variations(
    components: list[dict[str, str]],
    variant_details: configparser.SectionProxy,
    logger: Logger,
) -> list[dict[str, str]]:
    """
    Apply the variations of a specific variant to the components. This should be
    done before the components are mapped to columns or grouped.

    :param components: The components to apply the variations to.
    :param variant_details: The section of the config file dealing with a
        specific variant.

    :returns: The components with the variations applied. Each component will
        have a ``_fitted`` attribute (``"True"`` or ``"False"``) and a
        ``_variation_kind`` attribute (``"FITTED_MOD_PARAMS"``,
        ``"NOT_FITTED"``, ``"ALT_COMP"``, or ``""`` for unmodified components).
    """

    # Map of component UniqueID -> VariationKind for all explicitly varied components.
    components_variation_kind: dict[str, VariationKind] = {}
    # When patching components, the ParamVariation doesn't have the unique ID,
    # only a designator. However, ParamVariations follow the Variation entry, so
    # if we note down the last unique id we saw for a designator when going
    # through the variations, we can use that unique id when handling a param
    # variation. This dict holds that information.
    patch_component_unique_id: dict[str, str] = {}
    # The keys are the unique IDs, and the values are a key-value of the
    # parameter to patch and the value to patch it to.
    components_to_patch: dict[str, list[tuple[str, str]]] = {}

    for key, value in variant_details.items():
        # Note that this is in lowercase, as configparser stores all keys in
        # lowercase.
        if re.match(r"variation[\d+]", key):
            variation_details = dict(details.split("=", 1) for details in value.split("|"))
            try:
                designator = variation_details["Designator"]
                unique_id = variation_details["UniqueId"]
                kind = variation_details["Kind"]
            except KeyError:
                logger.warning(
                    "Designator, UniqueId, or Kind not found in details of variation "
                    f"{variation_details}; skipping this variation."
                )
                continue
            try:
                kind = VariationKind(int(variation_details["Kind"]))
            except ValueError:
                logger.warning(
                    f"Kind {variation_details['Kind']} of variation {variation_details} must be "
                    "either 0, 1 or 2; skipping this variation."
                )
                continue

            components_variation_kind[unique_id] = kind
            if kind != VariationKind.NOT_FITTED:
                patch_component_unique_id[designator] = unique_id
        elif re.match(r"paramvariation[\d]+", key):
            variation_id = key.split("paramvariation")[-1]
            designator = variant_details[f"ParamDesignator{variation_id}"]
            variation_details = dict(details.split("=", 1) for details in value.split("|"))
            try:
                unique_id = patch_component_unique_id[designator]
            except KeyError:
                # This can happen sometimes - Altium allows param variations
                # even when the component is not fitted, so we just log and
                # ignore.
                logger.warning(
                    f"ParamVariation{variation_id} found for component {designator} either before "
                    "the corresponding Variation or for a component that is not fitted.\n"
                    "Ignoring this ParamVariation."
                )
                continue

            try:
                parameter_patch = (
                    variation_details["ParameterName"],
                    variation_details["VariantValue"],
                )
            except KeyError:
                logger.warning(
                    f"ParameterName or VariantValue not found in ParamVariation{variation_id} "
                    "details."
                )
                continue

            components_to_patch.setdefault(unique_id, []).append(parameter_patch)

    # The PrjPcb and SchDoc JSON can store different unique IDs for the same
    # sheet symbol, causing full-path lookups to fail. Build a secondary index
    # by leaf ID (the component's own unique ID, which is consistent across
    # both files) as a fallback.
    components_variation_kind_by_leaf = {
        uid.rsplit("\\", 1)[-1]: kind
        for uid, kind in components_variation_kind.items()
    }
    components_to_patch_by_leaf = {
        uid.rsplit("\\", 1)[-1]: patches
        for uid, patches in components_to_patch.items()
    }

    final_components = []

    for component in components:
        unique_id = component["_unique_id"]
        leaf_id = unique_id.rsplit("\\", 1)[-1]

        kind = components_variation_kind.get(unique_id)
        if kind is None:
            kind = components_variation_kind_by_leaf.get(leaf_id)

        patches = components_to_patch.get(unique_id) or components_to_patch_by_leaf.get(leaf_id)

        new_component = component.copy()
        new_component["_fitted"] = "False" if kind == VariationKind.NOT_FITTED else "True"
        new_component["_variation_kind"] = kind.name if kind is not None else ""

        if patches:
            for parameter, value in patches:
                new_component[parameter] = value

        final_components.append(new_component)

    return final_components


def _filter_blank_components(
    components: list[ComponentAttributes],
    logger: Logger,
) -> list[ComponentAttributes]:
    """
    Remove components that have no attributes, or components for which all
    attributes are empty strings.

    This funtion also debug logs a warning for components that have no attributes.
    """

    final_components = []

    for component in components:
        if not any(component.values()):
            logger.debug(f"Component {component} has no attributes; skipping.")
            continue
        final_components.append(component)

    return final_components


def _find_device_sheet(
    device_sheet: str,
    project_repository: Repository,
    project_file_path: str,
    design_reuse_repos: list[Repository],
    logger: Logger,
) -> tuple[Repository, pathlib.PurePosixPath]:
    """
    Find a device sheet by name in the design reuse repositories.

    This currently does not use the directory structure of the device sheet
    path from the PrjPcb file, and instead only matches on the filename. If
    multiple matches are found, they are logged.

    :param design_reuse_repos: The design reuse repositories to search.
    :param device_sheet: The path of the device sheet as given in the PrjPcb.
    :returns: The first matching repository and the path to the device sheet in
        that repository.
    """

    matches: list[tuple[Repository, pathlib.PurePosixPath]] = []

    # Paths stored in the PrjPcb are Windows paths
    device_sheet_path = pathlib.PureWindowsPath(device_sheet)
    # Note that this will include the .SchDoc extension.
    device_sheet_name = device_sheet_path.name.casefold()

    # First, we'll resolve the actual path of the device sheet in the project
    # repo, and if that's a real file then we use that.
    device_sheet_path_from_repo_root = _resolve_prjpcb_relative_path(
        device_sheet,
        project_file_path,
    ).casefold()
    project_repo_tree = _fetch_all_files_in_repo(project_repository)
    for filepath in project_repo_tree:
        if device_sheet_path_from_repo_root == filepath.casefold():
            logger.info("Found device sheet %s in project repository; using that.", device_sheet)
            return (
                project_repository,
                pathlib.PurePosixPath(filepath),
            )

    for repo in design_reuse_repos:
        files_in_repo = _fetch_all_files_in_repo(repo)
        for file_in_repo in files_in_repo:
            # Paths as reported by ASH are Unix paths
            file_path = pathlib.PurePosixPath(file_in_repo)
            if device_sheet_name == file_path.name.casefold():
                matches.append((repo, file_path))

    if len(matches) == 0:
        raise ValueError(
            f"No matching device sheet found for {device_sheet_path} in the design reuse "
            "repositories.",
        )

    if len(matches) > 1:
        logger.info(
            "Multiple matches found for device sheet %s in design reuse repositories; set log "
            "level to debug to see all matches.",
            device_sheet,
        )
        for match in matches:
            logger.debug("Matching repository: %s, matching file path: %s", match[0].url, match[1])

        first_match = matches[0]
        logger.info(
            "Picking first match, i.e. Repository: %s, File Path: %s",
            first_match[0].url,
            first_match[1],
        )

    return matches[0]


def _fetch_and_parse_annotation_file(
    repository: Repository,
    prjpcb_path: str,
    ref: Ref,
) -> dict:
    """
    Determine the path to the annotation file, fetch it and parse it.

    :param repository: The repository containing the annotation file.
    :param prjpcb_path: The path to the PrjPcb file from the repo root.
    :param ref: The git ref to check.
    :returns: The parsed contents of the annotation file. The returned
        dictionary maps the full component unique id to a list of changes. For
        example, a component with id \\A\\B\\C with channel name CN that should
        change from C1 to C12 will be stored as
        {"\\A\\B\\C": [{"from": "C1", "to": "C12", "channel_name": "CN"}]}.
    """

    # According to the Altium documentation, the annotation file is stored with
    # the PrjPcb file and has the same name as the PrjPcb file, but with a
    # .Annotation extension.

    prjpcb_posix_path = pathlib.PurePosixPath(prjpcb_path)
    prjpcb_directory = prjpcb_posix_path.parent
    annotation_file_path = prjpcb_directory / (prjpcb_posix_path.stem + ".Annotation")
    caseless_annotation_file_path = annotation_file_path.as_posix().casefold()

    # We need to check case-insensitively because the project is made on
    # Windows, but ASH has a Unix-like filesystem.

    files_in_repo = _fetch_all_files_in_repo(repository)
    # Since we're checking case insensitively, we need to make sure there's only
    # one match for the annotation file path.
    matches = []
    for file_path in files_in_repo:
        if file_path.casefold() == caseless_annotation_file_path:
            matches.append(file_path)

    if len(matches) == 0:
        raise Exception(
            f"Could not find annotation file {annotation_file_path} in repository {repository.url}."
        )
    if len(matches) > 1:
        raise Exception(
            f"Multiple matches found for annotation file {annotation_file_path} in repository "
            f"{repository.url} when checking case-insensitively: {matches}."
        )

    annotation_file_path = matches[0]

    raw_annotation_content = repository.get_raw_file(annotation_file_path, ref=ref)
    try:
        annotation_file_contents = raw_annotation_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        annotation_file_contents = raw_annotation_content.decode("iso-8859-1")

    annotation_file_ini = configparser.ConfigParser(interpolation=None)
    annotation_file_ini.read_string(annotation_file_contents)

    designator_manager_section = annotation_file_ini["DesignatorManager"]
    changes = {}

    # We'll manually loop through change numbers until we find one which
    # doesn't exist.
    change_number = 0

    while True:
        unique_id_key = f"UniqueID{change_number}"
        if unique_id_key not in designator_manager_section:
            break
        unique_id = designator_manager_section[unique_id_key]
        logical_designator = designator_manager_section[f"LogicalDesignator{change_number}"]
        physical_designator = designator_manager_section[f"PhysicalDesignator{change_number}"]
        change_number += 1

        if unique_id in changes:
            repository.allspice_client.logger.warning("Multiple changes found for %s", unique_id)
            continue

        changes[unique_id] = {"from": logical_designator, "to": physical_designator}

    return changes


def _apply_annotation_file(
    components: list[ComponentAttributes],
    annotations_data: dict,
    unique_ids_mapping: dict[str, str],
    logger: Logger,
) -> list[ComponentAttributes]:
    """
    Apply a Board Level Annotations file to get the compnents with their final
    designators.
    """

    final_components = []

    # Map sheet id to dict of component id to change.
    annotations_by_sheet = {}
    # Map sheet id to dict of designator to list of changes.
    annotations_by_sheet_and_designator = {}

    for unique_id, change in annotations_data.items():
        sheet_id, component_id = unique_id.rsplit("\\", 1)
        annotations_by_sheet.setdefault(sheet_id, {})[component_id] = change
        annotations_by_sheet_and_designator.setdefault(sheet_id, {}).setdefault(
            change["from"], []
        ).append(change)

    for component in components:
        unique_id = component["_unique_id"]
        sheet_id, component_id = unique_id.rsplit("\\", 1)
        ids_to_test = [component_id]
        if component_id in unique_ids_mapping:
            logger.debug("Found component %s in unique ID mapping", component_id)
            other_component_ids = unique_ids_mapping[component_id]
            ids_to_test.extend(other_component_ids)

        annotation_for_component = None
        for id_to_test in ids_to_test:
            annotation_for_component = annotations_by_sheet.get(id_to_test)
            if annotation_for_component:
                break

        if not annotation_for_component:
            logger.debug(
                "No annotation found for component %s by unique ID, trying by designator.",
                component_id,
            )
            component_designator = component[LOGICAL_DESIGNATOR]
            annotations_for_designator = annotations_by_sheet_and_designator.get(sheet_id, {}).get(
                component_designator
            )
            if annotations_for_designator:
                # Collapse to unique mappings by converting dicts to tuples and back
                unique_annotations = [
                    dict(t) for t in {tuple(sorted(d.items())) for d in annotations_for_designator}
                ]

                if len(unique_annotations) == 1:
                    logger.debug(
                        "Found exact match for component %s, using annotation.",
                        component_designator,
                    )
                    annotation_for_component = unique_annotations[0]
                else:
                    logger.debug(
                        "Found multiple unique annotations for designator %s; skipping.",
                        component_designator,
                    )
            else:
                logger.debug(
                    "No annotations found for designator %s; skipping.", component_designator
                )

        if not annotation_for_component:
            final_components.append(component)
            continue

        new_component = component.copy()
        new_component[DESIGNATOR_COLUMN_NAME] = annotation_for_component["to"]
        if LOGICAL_DESIGNATOR in new_component:
            new_component[LOGICAL_DESIGNATOR] = annotation_for_component["to"]
        final_components.append(new_component)
        continue

    return final_components


def _compute_repetitions(components: list[ComponentAttributes]) -> list[ComponentAttributes]:
    """
    In the absence of an Annotation file, manually append suffixes to repeated
    components.
    """

    final_components = []

    # The rough logic here is that for all components with the same ID, i.e.
    # the LAST segment of their unique ID, we sort them by channel name and
    # suffix the index based on its position in the sorted list.

    components_by_id = {}

    for component in components:
        unique_id = component["_unique_id"]
        last_segment = unique_id.split("\\")[-1]
        components_by_id.setdefault(last_segment, []).append(component)

    for components in components_by_id.values():
        if len(components) == 1:
            final_components.append(components[0])
            continue

        # If there are multiple components with the same ID, they *must* be
        # from repeated sheets, so we they *must* have the _channel_name field
        # in the component attributes.
        components.sort(key=lambda component: component["_channel_name"])
        for i, component in enumerate(components):
            new_component = component.copy()
            new_component[DESIGNATOR_COLUMN_NAME] += _letters_for_repetition(i + 1)
            new_component[LOGICAL_DESIGNATOR] += _letters_for_repetition(i + 1)
            final_components.append(new_component)

    return final_components


def _correct_device_sheet_reference_unique_ids(
    hierarchy: SchdocHierarchy,
    prjpcb_ini: configparser.ConfigParser,
    logger: Logger,
) -> SchdocHierarchy:
    """
    When using device sheets, the UniqueIDs of the sheet references in Device
    sheets are updated by Altium to be unique to the project. All UniqueID
    paths to the files are stored in the PrjPcb file, so this function compares
    the hierarchy to the paths in the PrjPcb path to correct the UniqueIDs of
    the SheetRefs to be what they should be in the project.

    This is known to not work in the following cases:

    - There are multiple top sheets in the project. Altium warns in this case,
      so this is not supported.
    - There are multiple sheets with the same name in the project. Altium also
      doesn't support this well and will do unusual things, so we don't
      support this.
    """

    try:
        annotate_section = prjpcb_ini["Annotate"]
    except KeyError:
        logger.warning(
            "Annotate section not found in PrjPcb; Designators of componets within Device Sheets may be incorrect."
        )
        return hierarchy

    paths_to_documents: dict[str, list[str]] = {}
    document_index = 0

    while True:
        try:
            document_name = annotate_section[f"DocumentName{document_index}"]
            unique_id_path = annotate_section[f"UniqueIDPath{document_index}"]
        except KeyError:
            break

        document_index += 1

        if not unique_id_path:
            # The unique ID path can be blank for the top sheet - we'll just
            # skip it, because we already know the top sheet.
            continue

        paths_to_documents.setdefault(document_name, []).append(unique_id_path)

    # How this works:
    #
    # We have a list of paths which are based on the hierarchy of sheet ref
    # unique IDs, and a list of the sheetrefs that are on each sheet. We also
    # know that all instances of a sheet have the same SheetRefs with the same
    # IDs. So if a Sheet1.SchDoc has two children Sheet2.SchDoc and
    # Sheet3.SchDoc, it will have two sheet refs, and across all instances of
    # Sheet1.SchDoc those sheetrefs will have the same unique Id.
    #
    # Therefore, we can take one of the paths to Sheet1, which gives us the
    # path for an instance of Sheet1. Then we take the list of children of
    # Sheet1 and pick a child, say Sheet2, and find all paths to Sheet2 that
    # passes through this instance of Sheet1. We also filter out paths that
    # aren't immediate children of this instance, and so we'll have a list of
    # unique IDs for the sheet refs, which we then reconcile with the sheetrefs
    # we do have.

    changed_hierarchy = {}

    for sheet_name, children in hierarchy.items():
        if not sheet_name.endswith(".SchDoc"):
            sheet_name_with_extension = sheet_name + ".SchDoc"
        else:
            sheet_name_with_extension = sheet_name

        if sheet_name_with_extension not in paths_to_documents:
            changed_hierarchy[sheet_name] = children
            continue

        unique_id_paths = paths_to_documents[sheet_name_with_extension]
        # Since all instances of this sheet should have the same children, we
        # only need one of the unique paths.
        path_to_this_sheet = unique_id_paths[0]
        children_by_name: dict[str, list[AltiumChildSheet]] = {}

        final_children = []
        for child in children:
            if child.kind == AltiumChildSheet.ChildSheetKind.DOCUMENT:
                # We don't need to change the IDs of documents - they're
                # already correct.
                final_children.append(child)
                continue

            children_by_name.setdefault(child.name, []).append(child)

        for child_name, children_of_name in children_by_name.items():
            # The name in the paths_to_documents dict has a SchDoc appended to
            # it
            child_name += ".SchDoc"
            paths_to_children = paths_to_documents.get(child_name, [])

            if not paths_to_children:
                logger.warning(
                    f"Could not find any paths for {child_name} in the PrjPcb file; skipping."
                )
                final_children.extend(children_of_name)
                continue

            ids: set[str] = set()
            for path in paths_to_children:
                if not path.startswith(path_to_this_sheet):
                    continue

                path = path.removeprefix(path_to_this_sheet + "\\")

                if "\\" in path:
                    # This path is deeper in the hierarchy, we can't use it.
                    continue

                # Now the path is the UniqueID of the child sheet
                ids.add(path)

            # Filter out children that already have the correct ids.
            children_to_correct = []
            for child in children_of_name:
                if child.unique_id in ids:
                    ids.remove(child.unique_id)
                    final_children.append(child)
                else:
                    children_to_correct.append(child)

            # Now we have the children who need new ids and the new ids
            for child, unique_id in zip(children_to_correct, ids, strict=True):
                final_children.append(dataclasses.replace(child, unique_id=unique_id))

        changed_hierarchy[sheet_name] = final_children

    return changed_hierarchy


def _create_unique_ids_mapping(prjpcb_ini: configparser.ConfigParser) -> dict:
    """
    If multiple components in the same project have the same UniqueIDs, Altium
    creates a mapping between them. This function reads that mapping from the
    PrjPcb file.
    """

    if "UniqueIdsMappings" not in prjpcb_ini.sections():
        return {}

    unique_ids_mapping = {}
    unique_ids_section = prjpcb_ini["UniqueIdsMappings"]
    # this section starts with one for some reason
    mapping_index = 1

    while True:
        try:
            mapping = unique_ids_section[f"Mapping{mapping_index}"]
        except KeyError:
            break
        (sch_handle, unique_id_mapping) = mapping.split("|")
        _, sch_handle = sch_handle.split("=", 1)
        _, unique_id_mapping = unique_id_mapping.split("=", 1)
        # The sch handle is a path, we only need the last part of it.
        component_id = sch_handle.split("\\")[-1]
        unique_ids_mapping.setdefault(component_id, []).append(unique_id_mapping)
        mapping_index += 1

    return unique_ids_mapping

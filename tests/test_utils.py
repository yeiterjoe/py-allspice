import base64
import csv
import io
import os
from collections import Counter
from unittest.mock import patch

import pytest
from syrupy.extensions.json import JSONSnapshotExtension

from allspice import AllSpice
from allspice.exceptions import NotYetGeneratedException
from allspice.utils import list_components, retry_generated
from allspice.utils.bom_generation import (
    ColumnConfig,
    generate_bom,
    generate_bom_for_altium,
    generate_bom_for_dehdl,
    generate_bom_for_orcad,
    generate_bom_for_system_capture,
)
from allspice.utils.list_components import (
    _apply_variations,
    _combine_multi_part_components_for_dehdl,
    _resolve_prjpcb_relative_path,
    list_components_for_altium,
    list_components_for_orcad,
)
from allspice.utils.netlist_generation import generate_netlist

from .csv_snapshot_extension import CSVSnapshotExtension


@pytest.fixture(scope="session")
def port(pytestconfig):
    """Load --port command-line arg if set"""
    return pytestconfig.getoption("port")


@pytest.fixture(scope="session")
def client_log_level(pytestconfig):
    """Load --client-log-level command-line arg if set"""
    return pytestconfig.getoption("client_log_level")


def normalize_csv_row(row, skip_cols):
    """Sort a csv row by column and exclude colums to be skipped.
    Designator lists need to be individually sorted"""

    def normalize_value(k, v):
        v = v.strip() if v else ""
        if k.lower() == "designator" or k.lower() == "ref des" or k.lower() == "refdes":
            parts = [x.strip() for x in v.split(",")]
            parts.sort()
            return ", ".join(parts)
        elif k.lower() == "value":
            return v.lower()
        return v

    return tuple(
        sorted(
            (k.strip(), normalize_value(k.strip(), v))
            for k, v in row.items()
            if k.strip() not in skip_cols
        )
    )


def compare_golden_bom(golden_csv, generated_bom_rows, skip_cols):
    """Verify a generated BOM against a checked in BOM"""

    csv_stream = io.StringIO(golden_csv)

    assert Counter(
        [normalize_csv_row(row, skip_cols) for row in csv.DictReader(csv_stream)]
    ) == Counter([normalize_csv_row(row, skip_cols) for row in generated_bom_rows])


@pytest.fixture
def instance(port, client_log_level, pytestconfig):
    # The None record mode is the default and is equivalent to "once", but it
    # can also be set to None if recording is disabled entirely.
    if (
        pytestconfig.getoption("record_mode") in ["none", "once", None]
    ) and not pytestconfig.getoption("disable_recording"):
        # If we're using cassettes, we don't want BOM generation to sleep
        # between requests to wait for the generated JSON to be available.
        retry_generated.SLEEP_FOR_GENERATED = 0

    if os.environ.get("CI") == "true":
        # The CI runner is anemic and may not be able to generate the outputs
        # in the default number of retries, so we set it to a very high number
        # to make it effectively retry for a fair amount of time if we're not
        # using cassettes. If it cannot generate even in ~100s, that could
        # indicate a real issue.
        retry_generated.MAX_RETRIES_FOR_GENERATED = 100

    try:
        g = AllSpice(
            f"http://localhost:{port}",
            open(".token", "r").read().strip(),
            ratelimiting=None,
            log_level=client_log_level or "INFO",
        )
        print("AllSpice Hub Version: " + g.get_version())
        print("API-Token belongs to user: " + g.get_user().username)

        return g
    except Exception:
        assert False, (
            f"AllSpice Hub could not load. Is there: \
                - an Instance running at http://localhost:{port} \
                - a Token at .token \
                    ?"
        )


@pytest.fixture
def setup_for_generation(instance):
    repos = []

    def setup_for_generation_inner(test_name, clone_addr):
        # TODO: we should commit a smaller set of files in this repo so we don't
        #       depend on external data
        nonlocal repos

        instance.requests_post(
            "/repos/migrate",
            data={
                "clone_addr": clone_addr,
                "mirror": False,
                "repo_name": "-".join(["test", test_name]),
                "service": "git",
            },
        )

        repo = instance.get_repository(
            instance.get_user().username,
            "-".join(["test", test_name]),
        )
        repos.append(repo)
        return repo

    yield setup_for_generation_inner

    for repo in repos:
        repo.delete()


@pytest.fixture(scope="module")
def vcr_config():
    return {"filter_headers": ["authorization"]}


@pytest.fixture()
def json_snapshot(snapshot):
    return snapshot.use_extension(JSONSnapshotExtension)


@pytest.fixture()
def csv_snapshot(snapshot):
    return snapshot.use_extension(CSVSnapshotExtension)


@pytest.mark.vcr
def test_bom_generation_flat(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )

    assert len(bom) == 913

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_odd_line_endings(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    # We hard-code a ref so that this test is reproducible.
    ref = "95719adde8107958bf40467ee092c45b6ddaba00"
    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }

    new_branch_name = "-".join(["odd-line-endings", request.node.name])
    repo.add_branch(ref, new_branch_name)
    ref = new_branch_name

    files_in_repo = repo.get_git_content(ref=ref)
    prjpcb_file = next((x for x in files_in_repo if x.path == "Archimajor.PrjPcb"), None)
    assert prjpcb_file is not None

    original_prjpcb_sha = prjpcb_file.sha
    prjpcb_content = repo.get_raw_file(prjpcb_file.path, ref=ref).decode("utf-8")
    new_prjpcb_content = prjpcb_content.replace("\r\n", "\n\r")
    new_content_econded = base64.b64encode(new_prjpcb_content.encode("utf-8")).decode("utf-8")
    repo.change_file(
        "Archimajor.PrjPcb",
        original_prjpcb_sha,
        new_content_econded,
        {"branch": ref},
    )

    # Sanity check that the file was changed.
    prjpcb_content_now = repo.get_raw_file("Archimajor.PrjPcb", ref=ref).decode("utf-8")
    assert prjpcb_content_now != prjpcb_content

    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        # Note that ref here is the branch, not a commit sha as in the previous
        # test.
        ref=ref,
    )

    assert len(bom) == 913

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_grouped(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }

    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        group_by=["part_number", "manufacturer", "description"],
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )

    assert len(bom) == 108

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_folder_hierarchy(
    request,
    instance,
    setup_for_generation,
    csv_snapshot,
):
    """Test Altium BOM generation where design documents are in folders
    relative to the project file."""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorInFolders.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        group_by=["part_number"],
        # We hard-code a ref so that this test is reproducible.
        ref="e39ecf4de0c191559f5f23478c840ac2b6676d58",
    )

    assert len(bom) == 102
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_default_variant(request, instance, setup_for_generation, csv_snapshot):
    """Test Altium BOM generation with the default variant (not explicitly specified)"""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorVariants.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        ref="916e739f3ad9d956f4e2a293542050e1df9e6f9e",
        # For the variants tests, we don't want to remove non-BOM components
        # because some of them are enabled by the variants, and we want to
        # test that they are included when required.
        remove_non_bom_components=False,
    )

    # Since we haven't specified a variant, this should have the same result
    # as generating a flat BOM. This version of archimajor has a few parts
    # removed even before the variations, so the number of parts is different.
    assert len(bom) == 975

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_fitted_variant(request, instance, setup_for_generation, csv_snapshot):
    """Test Altium BOM generation with a non-default variant"""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorVariants.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        ref="916e739f3ad9d956f4e2a293542050e1df9e6f9e",
        variant="Fitted",
        remove_non_bom_components=False,
    )

    # Exactly 42 rows should be removed, as that is the number of non-param
    # variations.
    assert len(bom) == 975 - 42

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_grouped_variant(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorVariants.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        group_by=["part_number"],
        ref="916e739f3ad9d956f4e2a293542050e1df9e6f9e",
        variant="Fitted",
        remove_non_bom_components=False,
    )

    assert len(bom) == 89

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_with_non_bom_components(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
        remove_non_bom_components=False,
    )

    assert len(bom) == 1049

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_repeated_multi_part_component(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorRepeated.git",
    )
    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        ref="1bb73a0c862e156557e05876fb268ba086e9d42d",
        remove_non_bom_components=True,
    )

    assert len(bom) == 870

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_with_column_config(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )
    columns = {
        "description": ColumnConfig(
            attributes="PART DESCRIPTION",
            grouped_values_sort=ColumnConfig.SortOrder.DESC,
            grouped_values_allow_duplicates=True,
        ),
        "designator": ColumnConfig(
            attributes="Designator",
            grouped_values_sort=ColumnConfig.SortOrder.DESC,
            grouped_values_separator=";",
        ),
        "manufacturer": ColumnConfig(
            attributes=["Manufacturer", "MANUFACTURER"],
            sort=ColumnConfig.SortOrder.DESC,
        ),
        "tolerance": ColumnConfig(
            attributes=["Tolerance", "TOLERANCE"],
            sort=ColumnConfig.SortOrder.ASC,
            remove_rows_matching="-NA-",
            skip_in_output=True,
        ),
        "part_number": ColumnConfig(
            attributes=["PART", "MANUFACTURER #"],
            sort=ColumnConfig.SortOrder.ASC,
        ),
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        columns,
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
        group_by=["part_number"],
    )

    assert len(bom) == 64
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_repeated_multi_part_component_variant(
    request, instance, setup_for_generation, csv_snapshot
):
    """Test Altium BOM generation with a repeated multipart component as well
    as a non-default variant"""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorRepeatedVariant.git",
    )
    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        ref="3f8ddd6b5161aebc61a3ed87b665ba0a64cc6e89",
        variant="Fitted",
        remove_non_bom_components=True,
    )

    assert len(bom) == 869

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_with_device_sheets(
    request,
    instance,
    setup_for_generation,
    csv_snapshot,
):
    """Test Altium BOM generation with a design reuse repo."""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheet-Usage-Demo",
    )
    design_reuse_repo = setup_for_generation(
        request.node.name + "_reuse",
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheets",
    )
    attributes_mapping = {
        "Name": ["_name"],
        "Designator": ["Designator"],
        "Comment": ["Comment"],
    }
    bom = generate_bom_for_altium(
        instance,
        repo,
        "DCDC Regulators Breakout/DCDC Regulators Breakout.PrjPcb",
        attributes_mapping,
        group_by=["Comment"],
        design_reuse_repos=[design_reuse_repo],
        ref="5f2bdd30f57eb8ea6699dc9dcb098bc34d60f7a3",
    )

    assert len(bom) == 13
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_with_external_device_sheet(
    request,
    instance,
    setup_for_generation,
    csv_snapshot,
):
    """Test Altium BOM generation with design reuse against an Altium generated BOM.
    Note: the design reuse repo is added as a submodule for use in testing future
    submodule-based design reuse support."""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/Altium-Hierarchical-Device-Sheet-Usage-Demo",
    )

    design_reuse_repo = setup_for_generation(
        request.node.name + "_reuse",
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheets-Hierarchical-Repetitions",
    )
    attributes_mapping = {
        "Description": ["_description"],
        "Designator": ColumnConfig(
            attributes=["Designator"],
            grouped_values_sort=ColumnConfig.SortOrder.ASC,
        ),
        "Comment": ColumnConfig(attributes=["Comment"], sort=ColumnConfig.SortOrder.ASC),
        "LibRef": ["_part_id"],
    }

    bom = generate_bom_for_altium(
        instance,
        repo,
        "NestedDeviceSheets.PrjPcb",
        attributes_mapping,
        group_by=["Comment"],
        design_reuse_repos=[design_reuse_repo],
        ref="kd/generate-bom",
    )

    assert len(bom) == 14
    assert bom == csv_snapshot

    golden_bytes = repo.get_raw_file("NestedDeviceSheets.csv", ref="kd/generate-bom").decode(
        "windows-1252"
    )
    compare_golden_bom(golden_bytes, bom, ["Footprint"])


@pytest.mark.vcr
def test_bom_generation_altium_with_managed_sheets(
    request,
    instance,
    setup_for_generation,
    csv_snapshot,
):
    """Test Altium BOM generation against a project that uses managed sheets.
    These have a different directory structure than normal device sheet refs."""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ThunderScope.git",
    )

    attributes_mapping = {
        "Part Number": ["Manufacturer Part Number"],
        "Designator": ColumnConfig(
            attributes=["Designator"],
            grouped_values_sort=ColumnConfig.SortOrder.ASC,
        ),
        "Comment": ColumnConfig(attributes=["Comment"], sort=ColumnConfig.SortOrder.ASC),
        "LibRef": ["_part_id"],
    }

    bom = generate_bom_for_altium(
        instance,
        repo,
        "Hardware/Altium/Thunderscope_Rev5/Thunderscope_Rev5.PrjPCB",
        attributes_mapping,
        group_by=["Part Number"],
        ref="2cb843cb78818ca94896b84907f5fe4c2df6bf30",
    )

    assert len(bom) == 83
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_orcad(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/beagleplay.git",
    )

    attributes_mapping = {
        "Name": ["_name"],
        "Description": "Description",
        "Reference designator": ["Part Reference"],
        "Manufacturer": ["Manufacturer", "MANUFACTURER"],
        "Part Number": ["Manufacturer PN", "PN"],
    }

    bom = generate_bom_for_orcad(
        instance,
        repo,
        "Design/BEAGLEPLAYV10_221227.DSN",
        attributes_mapping,
        # We hard-code a ref so that this test is reproducible.
        ref="7a59a98ae27dc4fd9e2bd8975ff90cdb44a366ea",
    )

    assert len(bom) == 846

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_orcad_with_column_config(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/AllSpiceMirrors/beagleplay.git",
    )
    columns = {
        "Manufacturer": ColumnConfig(
            attributes=["Manufacturer", "MANUFACTURER"],
            sort=ColumnConfig.SortOrder.ASC,
        ),
        "Reference designator": "Part Reference",
        "Name": ColumnConfig(
            attributes="_name",
            grouped_values_allow_duplicates=False,
            grouped_values_sort=ColumnConfig.SortOrder.ASC,
        ),
        "Description": ColumnConfig(
            attributes="Description",
            grouped_values_sort=ColumnConfig.SortOrder.DESC,
            grouped_values_separator=";",
        ),
        "Part Number": ColumnConfig(
            attributes=["Manufacturer PN", "PN"],
            remove_rows_matching="^TP",
        ),
    }
    bom = generate_bom_for_orcad(
        instance,
        repo,
        "Design/BEAGLEPLAYV10_221227.DSN",
        columns,
        ref="7a59a98ae27dc4fd9e2bd8975ff90cdb44a366ea",
    )

    assert len(bom) == 777
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_system_capture(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/parallela-sdax.git",
    )

    attributes_mapping = {
        "Description": "VALUE",
        "Designator": ["LOCATION"],
        "Part Number": ["VENDOR_PN", "PN"],
    }
    bom = generate_bom_for_system_capture(
        instance,
        repo,
        "parallella_schematic.sdax",
        attributes_mapping,
        # We hard-code a ref so that this test is reproducible.
        ref="e03461e6bbe72f10b163462cf9325b0309e87201",
    )

    assert len(bom) == 551
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_system_capture_variants(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/parallela-sdax.git",
    )

    attributes_mapping = {
        "Description": "VALUE",
        "Designator": ["LOCATION"],
        "Part Number": ["VENDOR_PN", "PN"],
        "Name": ["NAME", "value"],
    }
    bom = generate_bom_for_system_capture(
        instance,
        repo,
        "variant-test-1.sdax",
        attributes_mapping,
        # We hard-code a ref so that this test is reproducible.
        ref="ac41c9dc9aaa5acb215f3cc77f453bd754b49a8b",
        variant="TESTVAR",
    )

    assert len(bom) == 12
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_system_capture_grouped_failure(request, instance, setup_for_generation):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/parallela-sdax.git",
    )
    with pytest.raises(ValueError, match="Group by column Name not found in selected columns"):
        generate_bom_for_system_capture(
            instance,
            repo,
            "parallella_schematic.sdax",
            {},
            group_by=["Name"],
        )


def test_combine_multi_part_components_for_dehdl():
    """Test that multi-part components with the same LOCATION are combined correctly."""
    # Test data with components having the same LOCATION
    components = [
        {"LOCATION": "UT4", "CHIPS_PART_NAME": "RES_0402", "OTHER": "1"},
        {"LOCATION": "UT4", "CHIPS_PART_NAME": "RES_0402", "OTHER": "2"},
        {"LOCATION": "UT4", "CHIPS_PART_NAME": "RES_0402", "OTHER": "3"},
        {"LOCATION": "UT7", "CHIPS_PART_NAME": "CAP_0603", "OTHER": "1"},
        {"LOCATION": "UT7", "CHIPS_PART_NAME": "CAP_0603", "OTHER": "2"},
        {"LOCATION": "R1", "CHIPS_PART_NAME": "RES_0603", "OTHER": "1"},
        {"LOCATION": "C1", "CHIPS_PART_NAME": "CAP_0402", "OTHER": "1"},
        {"LOCATION": "", "CHIPS_PART_NAME": "RES_0402", "VALUE": "10K"},
        {"CHIPS_PART_NAME": "CAP_0603", "VALUE": "100nF"},
    ]

    result = _combine_multi_part_components_for_dehdl(components)

    # Should have 4 unique components (UT4, UT7, R1, C1)
    assert len(result) == 4
    locations = {comp["LOCATION"] for comp in result}
    assert locations == {"UT4", "UT7", "R1", "C1"}


def _make_variant_section(variations: dict[str, str]) -> "configparser.SectionProxy":
    """Build a configparser.SectionProxy from a plain dict for use in _apply_variations tests."""
    import configparser

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"Variant": variations})
    return cfg["Variant"]


def test_apply_variations_not_fitted_tags_component():
    """NOT_FITTED components are kept but tagged with _fitted=False and _variation_kind=NOT_FITTED."""
    import logging

    components = [
        {"_unique_id": "UID-R1", "Designator": "R1", "PART_NUMBER": "RES-100"},
        {"_unique_id": "UID-C1", "Designator": "C1", "PART_NUMBER": "CAP-10N"},
    ]
    variant_section = _make_variant_section(
        {"Variation1": "Designator=R1|UniqueId=UID-R1|Kind=1|AlternatePart="}
    )

    result = _apply_variations(components, variant_section, logging.getLogger())

    assert len(result) == 2

    r1 = next(c for c in result if c["_unique_id"] == "UID-R1")
    assert r1["_fitted"] == "False"
    assert r1["_variation_kind"] == "NOT_FITTED"
    # Original attributes preserved
    assert r1["PART_NUMBER"] == "RES-100"

    c1 = next(c for c in result if c["_unique_id"] == "UID-C1")
    assert c1["_fitted"] == "True"
    assert c1["_variation_kind"] == ""


def test_apply_variations_alt_comp_tags_component_and_patches_params():
    """ALT_COMP components are fitted=True with _variation_kind=ALT_COMP and params overridden."""
    import logging

    components = [
        {"_unique_id": "UID-R1", "Designator": "R1", "PART_NUMBER": "RES-100"},
    ]
    variant_section = _make_variant_section(
        {
            "Variation1": "Designator=R1|UniqueId=UID-R1|Kind=2|AlternatePart=RES-200",
            "ParamDesignator1": "R1",
            "ParamVariation1": "ParameterName=PART_NUMBER|VariantValue=RES-200",
        }
    )

    result = _apply_variations(components, variant_section, logging.getLogger())

    assert len(result) == 1
    r1 = result[0]
    assert r1["_fitted"] == "True"
    assert r1["_variation_kind"] == "ALT_COMP"
    assert r1["PART_NUMBER"] == "RES-200"


def test_apply_variations_fitted_mod_params_tags_component():
    """FITTED_MOD_PARAMS (Kind=0) components are fitted=True with _variation_kind=FITTED_MOD_PARAMS."""
    import logging

    components = [
        {"_unique_id": "UID-R1", "Designator": "R1", "PART_NUMBER": "RES-100"},
    ]
    variant_section = _make_variant_section(
        {
            "Variation1": "Designator=R1|UniqueId=UID-R1|Kind=0|AlternatePart=",
            "ParamDesignator1": "R1",
            "ParamVariation1": "ParameterName=PART_NUMBER|VariantValue=RES-150",
        }
    )

    result = _apply_variations(components, variant_section, logging.getLogger())

    assert len(result) == 1
    r1 = result[0]
    assert r1["_fitted"] == "True"
    assert r1["_variation_kind"] == "FITTED_MOD_PARAMS"
    assert r1["PART_NUMBER"] == "RES-150"


def test_apply_variations_sheet_uid_mismatch():
    """Leaf-ID fallback matches when PrjPcb sheet UID differs from SchDoc JSON sheet UID."""
    import logging

    # Component _unique_id uses DBWIPUYZ (from SchDoc JSON)
    # Variation UniqueId uses GFJQJVVU (from PrjPcb) — same leaf, different parent
    components = [
        {"_unique_id": "\\DBWIPUYZ\\SMWDAPJN", "Designator": "R14", "PART_NUMBER": "RES-100"},
        {"_unique_id": "\\DBWIPUYZ\\IIQUFYYE", "Designator": "C15", "PART_NUMBER": "CAP-10N"},
    ]
    variant_section = _make_variant_section(
        {
            "Variation1": "Designator=R14|UniqueId=\\GFJQJVVU\\SMWDAPJN|Kind=1|AlternatePart=",
            "Variation2": "Designator=C15|UniqueId=\\GFJQJVVU\\IIQUFYYE|Kind=1|AlternatePart=",
        }
    )

    result = _apply_variations(components, variant_section, logging.getLogger())

    assert len(result) == 2
    r14 = next(c for c in result if c["Designator"] == "R14")
    c15 = next(c for c in result if c["Designator"] == "C15")
    assert r14["_fitted"] == "False"
    assert r14["_variation_kind"] == "NOT_FITTED"
    assert c15["_fitted"] == "False"
    assert c15["_variation_kind"] == "NOT_FITTED"


def test_apply_variations_multi_digit_variation_numbers():
    """Variation keys with numbers >= 10 must be parsed (regression: [\\d+] only matched one digit)."""
    import logging

    components = [{"_unique_id": f"UID-{i}", "Designator": f"R{i}", "PART_NUMBER": "RES"} for i in range(1, 15)]
    # Variation10 through Variation13 are NOT_FITTED
    variant_section = _make_variant_section(
        {f"Variation{i}": f"Designator=R{i}|UniqueId=UID-{i}|Kind=1|AlternatePart=" for i in range(10, 14)}
    )

    result = _apply_variations(components, variant_section, logging.getLogger())

    assert len(result) == 14
    for i in range(10, 14):
        comp = next(c for c in result if c["_unique_id"] == f"UID-{i}")
        assert comp["_fitted"] == "False", f"R{i} should be NOT_FITTED"
    for i in list(range(1, 10)) + list(range(14, 15)):
        comp = next(c for c in result if c["_unique_id"] == f"UID-{i}")
        assert comp["_fitted"] == "True", f"R{i} should be fitted"


def test_generate_bom_excludes_not_fitted_by_default():
    """With include_not_fitted=False (default), DNP components are excluded from the BOM output."""
    from unittest.mock import MagicMock, patch

    from allspice.utils.bom_generation import generate_bom

    fitted_component = {"_unique_id": "UID-R1", "_fitted": "True", "_variation_kind": "", "PART_NUMBER": "RES-100"}
    dnp_component = {"_unique_id": "UID-C1", "_fitted": "False", "_variation_kind": "NOT_FITTED", "PART_NUMBER": "CAP-10N"}

    mock_client = MagicMock()
    mock_repo = MagicMock()

    with patch("allspice.utils.bom_generation.list_components", return_value=[fitted_component, dnp_component]):
        with patch("allspice.utils.bom_generation.infer_project_tool", return_value=MagicMock()):
            bom = generate_bom(
                mock_client,
                mock_repo,
                "test.PrjPcb",
                columns={"Part Number": "_fitted"},
                remove_non_bom_components=False,
            )

    assert len(bom) == 1
    assert bom[0]["Part Number"] == "True"


def test_generate_bom_includes_not_fitted_when_requested():
    """With include_not_fitted=True, DNP components appear in the BOM with _fitted=False."""
    from unittest.mock import MagicMock, patch

    from allspice.utils.bom_generation import generate_bom

    fitted_component = {"_unique_id": "UID-R1", "_fitted": "True", "_variation_kind": "", "PART_NUMBER": "RES-100"}
    dnp_component = {"_unique_id": "UID-C1", "_fitted": "False", "_variation_kind": "NOT_FITTED", "PART_NUMBER": "CAP-10N"}

    mock_client = MagicMock()
    mock_repo = MagicMock()

    with patch("allspice.utils.bom_generation.list_components", return_value=[fitted_component, dnp_component]):
        with patch("allspice.utils.bom_generation.infer_project_tool", return_value=MagicMock()):
            bom = generate_bom(
                mock_client,
                mock_repo,
                "test.PrjPcb",
                columns={"Fitted": "_fitted", "Variation": "_variation_kind"},
                remove_non_bom_components=False,
                include_not_fitted=True,
            )

    assert len(bom) == 2
    fitted_rows = [r for r in bom if r["Fitted"] == "True"]
    dnp_rows = [r for r in bom if r["Fitted"] == "False"]
    assert len(fitted_rows) == 1
    assert len(dnp_rows) == 1
    assert dnp_rows[0]["Variation"] == "NOT_FITTED"


@pytest.mark.vcr
def test_bom_generation_dehdl_uob(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/DeHDL-uob-hep-pc072.git",
    )

    attributes_mapping = {
        "REFDES": "LOCATION",
        # Note: we ignore COMP_DEVICE_TYPE in the final BOM but use it to remove
        # rows from the bom and to group components
        "COMP_DEVICE_TYPE": ColumnConfig(
            attributes=["CDS_PART_NAME", "_name"],
            remove_rows_matching="^(02_|08_|03_|05 |01_|07_|A3-2000|2-pin_jumper|vcc_bar|offpageleft-l|gnd|CTAP|P2V5|portleft-l|6 MERGE|portboth-r|IOPORT|cmntgrphs4|9 MERGE|AVDD|portboth-r_1|BUSWIDE_BTM_LEFT_RIP|BUSWIDE_TOP_LEFT_RIP|04_|portboth-l|BUS_TOP_LEFT_RIP|portright-l|4 MERGE).*",
        ),
    }

    bom = generate_bom_for_dehdl(
        instance,
        repo,
        "hardware/Cadence/top/top_mib_v3.cpm",
        attributes_mapping,
        group_by=["COMP_DEVICE_TYPE"],
        ref="main",
        remove_non_bom_components=False,
    )

    # Verify the generated BOM against the golden BOM
    golden_csv_content = repo.get_raw_file("bom.csv", ref="main").decode("utf-8")
    compare_golden_bom(
        golden_csv_content,
        bom,
        ["SYM_NAME", "COMP_VALUE", "COMP_TOL", "COMP_CLASS", "COMP_DEVICE_TYPE"],
        # Note: COMP_DEVICE_TYPE is an imperfect match with CDS_PART_NAME and
        # CDS_PHYS_PART_NAME for the current BOM version
    )

    assert bom == csv_snapshot


# The following tests are for the generate_bom function, which is a wrapper
# around the more specific functions for each EDA tool. We test the specific
# functions above, so these tests are just to make sure the wrapper works as
# expected.
@pytest.mark.vcr
def test_generate_bom_altium(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    altium_attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    bom = generate_bom(
        instance,
        repo,
        "Archimajor.PrjPcb",
        altium_attributes_mapping,
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )
    assert len(bom) == 913
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_generate_bom_orcad(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/beagleplay.git",
    )
    orcad_attributes_mapping = {
        "Name": ["_name"],
        "Description": "Description",
        "Reference designator": ["Part Reference"],
        "Manufacturer": ["Manufacturer", "MANUFACTURER"],
        "Part Number": ["Manufacturer PN", "PN"],
    }
    bom = generate_bom(
        instance,
        repo,
        "Design/BEAGLEPLAYV10_221227.DSN",
        orcad_attributes_mapping,
        ref="7a59a98ae27dc4fd9e2bd8975ff90cdb44a366ea",
    )
    assert len(bom) == 846
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_generate_bom_system_capture(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/parallela-sdax.git",
    )
    system_capture_attributes_mapping = {
        "Description": "VALUE",
        "Designator": ["LOCATION"],
        "Part Number": ["VENDOR_PN", "PN"],
    }
    bom = generate_bom(
        instance,
        repo,
        "parallella_schematic.sdax",
        system_capture_attributes_mapping,
        ref="e03461e6bbe72f10b163462cf9325b0309e87201",
    )
    assert len(bom) == 551
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_orcad_components_list(request, instance, setup_for_generation, json_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/beagleplay.git",
    )

    components = list_components_for_orcad(
        instance,
        repo,
        "Design/BEAGLEPLAYV10_221227.DSN",
        # We hard-code a ref so that this test is reproducible.
        ref="7a59a98ae27dc4fd9e2bd8975ff90cdb44a366ea",
    )

    assert len(components) == 870
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list(request, instance, setup_for_generation, json_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    components = list_components_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )

    assert len(components) == 1061
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_folder_hierarchy(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorInFolders.git",
    )

    components = list_components_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        # We hard-code a ref so that this test is reproducible.
        ref="e39ecf4de0c191559f5f23478c840ac2b6676d58",
    )

    assert len(components) == 1049
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_fitted_variant(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorVariants.git",
    )

    components = list_components_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        # We hard-code a ref so that this test is reproducible.
        ref="fbde2fe9fb7576c7e32827368224ec18717a1ffe",
        variant="Fitted",
    )

    assert len(components) == 945
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_device_sheets(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheet-Usage-Demo",
    )
    design_reuse_repo = setup_for_generation(
        request.node.name + "_reuse",
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheets",
    )
    components = list_components_for_altium(
        instance,
        repo,
        "DCDC Regulators Breakout/DCDC Regulators Breakout.PrjPcb",
        design_reuse_repos=[design_reuse_repo],
        ref="5f2bdd30f57eb8ea6699dc9dcb098bc34d60f7a3",
    )

    assert len(components) == 38
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_annotations(
    request, instance, setup_for_generation, json_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/FlatSat",
    )
    components = list_components_for_altium(
        instance,
        repo,
        "FlatSat/FlatSat.PrjPCB",
        ref="471d42ba87032682c7dc7a0235ffcc02808a3e37",
    )

    assert len(components) == 408
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_hierarchical_device_sheets_and_annotations(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/Altium-Hierarchical-Device-Sheet-Repetitions-Demo",
    )
    design_reuse_repo = setup_for_generation(
        request.node.name + "_reuse",
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheets-Hierarchical-Repetitions",
    )

    components = list_components_for_altium(
        instance,
        repo,
        "NestedDeviceSheets.PrjPcb",
        design_reuse_repos=[design_reuse_repo],
    )

    components.sort(key=lambda x: x["Designator"])
    assert len(components) == 980
    assert components == json_snapshot


@pytest.mark.vcr
def test_system_capture_components_list(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/parallela-sdax.git",
    )
    components = list_components.list_components_for_system_capture(
        instance,
        repo,
        "parallella_schematic.sdax",
        # We hard-code a ref so that this test is reproducible.
        ref="e03461e6bbe72f10b163462cf9325b0309e87201",
    )
    assert len(components) == 564
    assert components == json_snapshot


# The following tests are for the list_components function, which is a wrapper
# around the more specific functions for each EDA tool. We test the specific
# functions above, so these tests are just to make sure the wrapper works as
# expected.


@pytest.mark.vcr
def test_list_components_system_capture(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/parallela-sdax.git",
    )
    components = list_components.list_components(
        instance,
        repo,
        "parallella_schematic.sdax",
        # We hard-code a ref so that this test is reproducible.
        ref="e03461e6bbe72f10b163462cf9325b0309e87201",
    )
    assert len(components) == 564
    assert components == json_snapshot


@pytest.mark.vcr
def test_list_components_system_capture_with_variant(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/parallela-sdax.git",
    )
    components = list_components.list_components(
        instance,
        repo,
        "variant-test-1.sdax",
        # We hard-code a ref so that this test is reproducible.
        ref="ac41c9dc9aaa5acb215f3cc77f453bd754b49a8b",
        variant="TESTVAR",
    )
    assert len(components) == 12
    assert components == json_snapshot


def test_list_components_retries_time_out(
    request,
    instance,
    setup_for_generation,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/parallela-sdax.git",
    )

    with patch.object(repo, "get_generated_json", side_effect=NotYetGeneratedException):
        with pytest.raises(TimeoutError):
            list_components.list_components(
                instance,
                repo,
                "parallella_schematic.sdax",
            )


@pytest.mark.vcr
def test_netlist_generation(request, instance, setup_for_generation):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    netlist = generate_netlist(
        instance,
        repo,
        "Archimajor.PcbDoc",
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )
    assert len(netlist) == 682

    nets = list(netlist.keys())

    nets.sort()

    with open("tests/data/archimajor_netlist_expected.net", "r") as f:
        for net in nets:
            assert (net + "\n") == f.readline()
            pins_on_net = sorted(netlist[net])
            assert (" " + " ".join(pins_on_net) + "\n") == f.readline()


def test_resolve_prjpcb_relative_path():
    assert (
        _resolve_prjpcb_relative_path("..\\device-sheets\\sheet.SchDoc", "Project/Project.PrjPCB")
        == "device-sheets/sheet.SchDoc"
    )


# Legacy tests: these test the old Altium schdoc renderer by setting
# use_new_schdoc_renderer = False on the instance.
# They are a temporary solution to ensure backwards compatibility until we remove support
# for the legacy renderer.


@pytest.mark.vcr
def test_bom_generation_flat_legacy(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )

    assert len(bom) == 913

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_odd_line_endings_legacy(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    # We hard-code a ref so that this test is reproducible.
    ref = "95719adde8107958bf40467ee092c45b6ddaba00"
    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }

    new_branch_name = "-".join(["odd-line-endings", request.node.name])
    repo.add_branch(ref, new_branch_name)
    ref = new_branch_name

    files_in_repo = repo.get_git_content(ref=ref)
    prjpcb_file = next((x for x in files_in_repo if x.path == "Archimajor.PrjPcb"), None)
    assert prjpcb_file is not None

    original_prjpcb_sha = prjpcb_file.sha
    prjpcb_content = repo.get_raw_file(prjpcb_file.path, ref=ref).decode("utf-8")
    new_prjpcb_content = prjpcb_content.replace("\r\n", "\n\r")
    new_content_econded = base64.b64encode(new_prjpcb_content.encode("utf-8")).decode("utf-8")
    repo.change_file(
        "Archimajor.PrjPcb",
        original_prjpcb_sha,
        new_content_econded,
        {"branch": ref},
    )

    # Sanity check that the file was changed.
    prjpcb_content_now = repo.get_raw_file("Archimajor.PrjPcb", ref=ref).decode("utf-8")
    assert prjpcb_content_now != prjpcb_content

    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        # Note that ref here is the branch, not a commit sha as in the previous
        # test.
        ref=ref,
    )

    assert len(bom) == 913

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_grouped_legacy(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }

    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        group_by=["part_number", "manufacturer", "description"],
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )

    assert len(bom) == 108

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_folder_hierarchy_legacy(
    request,
    instance,
    setup_for_generation,
    csv_snapshot,
):
    """Test Altium BOM generation where design documents are in folders
    relative to the project file."""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorInFolders.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        group_by=["part_number"],
        # We hard-code a ref so that this test is reproducible.
        ref="e39ecf4de0c191559f5f23478c840ac2b6676d58",
    )

    assert len(bom) == 102
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_default_variant_legacy(
    request, instance, setup_for_generation, csv_snapshot
):
    """Test Altium BOM generation with the default variant (not explicitly specified)"""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorVariants.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        ref="916e739f3ad9d956f4e2a293542050e1df9e6f9e",
        # For the variants tests, we don't want to remove non-BOM components
        # because some of them are enabled by the variants, and we want to
        # test that they are included when required.
        remove_non_bom_components=False,
    )

    # Since we haven't specified a variant, this should have the same result
    # as generating a flat BOM. This version of archimajor has a few parts
    # removed even before the variations, so the number of parts is different.
    assert len(bom) == 975

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_fitted_variant_legacy(
    request, instance, setup_for_generation, csv_snapshot
):
    """Test Altium BOM generation with a non-default variant"""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorVariants.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        ref="916e739f3ad9d956f4e2a293542050e1df9e6f9e",
        variant="Fitted",
        remove_non_bom_components=False,
    )

    # Exactly 42 rows should be removed, as that is the number of non-param
    # variations.
    assert len(bom) == 975 - 42

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_with_grouped_variant_legacy(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorVariants.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        group_by=["part_number"],
        ref="916e739f3ad9d956f4e2a293542050e1df9e6f9e",
        variant="Fitted",
        remove_non_bom_components=False,
    )

    assert len(bom) == 89

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_with_non_bom_components_legacy(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
        remove_non_bom_components=False,
    )

    assert len(bom) == 1049

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_repeated_multi_part_component_legacy(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorRepeated.git",
    )
    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        ref="1bb73a0c862e156557e05876fb268ba086e9d42d",
        remove_non_bom_components=True,
    )

    assert len(bom) == 870

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_with_column_config_legacy(
    request, instance, setup_for_generation, csv_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )
    columns = {
        "description": ColumnConfig(
            attributes="PART DESCRIPTION",
            grouped_values_sort=ColumnConfig.SortOrder.DESC,
            grouped_values_allow_duplicates=True,
        ),
        "designator": ColumnConfig(
            attributes="Designator",
            grouped_values_sort=ColumnConfig.SortOrder.DESC,
            grouped_values_separator=";",
        ),
        "manufacturer": ColumnConfig(
            attributes=["Manufacturer", "MANUFACTURER"],
            sort=ColumnConfig.SortOrder.DESC,
        ),
        "tolerance": ColumnConfig(
            attributes=["Tolerance", "TOLERANCE"],
            sort=ColumnConfig.SortOrder.ASC,
            remove_rows_matching="-NA-",
            skip_in_output=True,
        ),
        "part_number": ColumnConfig(
            attributes=["PART", "MANUFACTURER #"],
            sort=ColumnConfig.SortOrder.ASC,
        ),
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        columns,
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
        group_by=["part_number"],
    )

    assert len(bom) == 64
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_repeated_multi_part_component_variant_legacy(
    request, instance, setup_for_generation, csv_snapshot
):
    """Test Altium BOM generation with a repeated multipart component as well
    as a non-default variant"""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorRepeatedVariant.git",
    )
    attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        attributes_mapping,
        ref="3f8ddd6b5161aebc61a3ed87b665ba0a64cc6e89",
        variant="Fitted",
        remove_non_bom_components=True,
    )

    assert len(bom) == 869

    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_with_device_sheets_legacy(
    request,
    instance,
    setup_for_generation,
    csv_snapshot,
):
    """Test Altium BOM generation with a design reuse repo."""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheet-Usage-Demo",
    )
    design_reuse_repo = setup_for_generation(
        request.node.name + "_reuse",
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheets",
    )
    attributes_mapping = {
        "Name": ["_name"],
        "Designator": ["Designator"],
        "Comment": ["Comment"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "DCDC Regulators Breakout/DCDC Regulators Breakout.PrjPcb",
        attributes_mapping,
        group_by=["Comment"],
        design_reuse_repos=[design_reuse_repo],
        ref="5f2bdd30f57eb8ea6699dc9dcb098bc34d60f7a3",
    )

    assert len(bom) == 13
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_bom_generation_altium_with_external_device_sheet_legacy(
    request,
    instance,
    setup_for_generation,
    csv_snapshot,
):
    """Test Altium BOM generation with design reuse against an Altium generated BOM.
    Note: the design reuse repo is added as a submodule for use in testing future
    submodule-based design reuse support."""
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/Altium-Hierarchical-Device-Sheet-Usage-Demo",
    )

    design_reuse_repo = setup_for_generation(
        request.node.name + "_reuse",
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheets-Hierarchical-Repetitions",
    )
    attributes_mapping = {
        "Description": ["_description"],
        "Designator": ColumnConfig(
            attributes=["Designator"],
            grouped_values_sort=ColumnConfig.SortOrder.ASC,
        ),
        "Comment": ColumnConfig(attributes=["Comment"], sort=ColumnConfig.SortOrder.ASC),
        "LibRef": ["_part_id"],
    }

    instance.use_new_schdoc_renderer = False
    bom = generate_bom_for_altium(
        instance,
        repo,
        "NestedDeviceSheets.PrjPcb",
        attributes_mapping,
        group_by=["Comment"],
        design_reuse_repos=[design_reuse_repo],
        ref="kd/generate-bom",
    )

    assert len(bom) == 14
    assert bom == csv_snapshot

    golden_bytes = repo.get_raw_file("NestedDeviceSheets.csv", ref="kd/generate-bom").decode(
        "windows-1252"
    )
    compare_golden_bom(golden_bytes, bom, ["Footprint"])


@pytest.mark.vcr
def test_generate_bom_altium_legacy(request, instance, setup_for_generation, csv_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    altium_attributes_mapping = {
        "description": ["PART DESCRIPTION"],
        "designator": ["Designator"],
        "manufacturer": ["Manufacturer", "MANUFACTURER"],
        "part_number": ["PART", "MANUFACTURER #"],
    }
    instance.use_new_schdoc_renderer = False
    bom = generate_bom(
        instance,
        repo,
        "Archimajor.PrjPcb",
        altium_attributes_mapping,
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )
    assert len(bom) == 913
    assert bom == csv_snapshot


@pytest.mark.vcr
def test_altium_components_list_legacy(request, instance, setup_for_generation, json_snapshot):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorDemo.git",
    )

    instance.use_new_schdoc_renderer = False
    components = list_components_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        # We hard-code a ref so that this test is reproducible.
        ref="95719adde8107958bf40467ee092c45b6ddaba00",
    )

    assert len(components) == 1061
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_folder_hierarchy_legacy(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorInFolders.git",
    )

    instance.use_new_schdoc_renderer = False
    components = list_components_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        # We hard-code a ref so that this test is reproducible.
        ref="e39ecf4de0c191559f5f23478c840ac2b6676d58",
    )

    assert len(components) == 1049
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_fitted_variant_legacy(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/ArchimajorVariants.git",
    )

    instance.use_new_schdoc_renderer = False
    components = list_components_for_altium(
        instance,
        repo,
        "Archimajor.PrjPcb",
        # We hard-code a ref so that this test is reproducible.
        ref="fbde2fe9fb7576c7e32827368224ec18717a1ffe",
        variant="Fitted",
    )

    assert len(components) == 953
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_device_sheets_legacy(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheet-Usage-Demo",
    )
    design_reuse_repo = setup_for_generation(
        request.node.name + "_reuse",
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheets",
    )
    instance.use_new_schdoc_renderer = False
    components = list_components_for_altium(
        instance,
        repo,
        "DCDC Regulators Breakout/DCDC Regulators Breakout.PrjPcb",
        design_reuse_repos=[design_reuse_repo],
        ref="5f2bdd30f57eb8ea6699dc9dcb098bc34d60f7a3",
    )

    assert len(components) == 38
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_annotations_legacy(
    request, instance, setup_for_generation, json_snapshot
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/FlatSat",
    )
    instance.use_new_schdoc_renderer = False
    components = list_components_for_altium(
        instance,
        repo,
        "FlatSat/FlatSat.PrjPCB",
        ref="471d42ba87032682c7dc7a0235ffcc02808a3e37",
    )

    assert len(components) == 408
    assert components == json_snapshot


@pytest.mark.vcr
def test_altium_components_list_with_hierarchical_device_sheets_and_annotations_legacy(
    request,
    instance,
    setup_for_generation,
    json_snapshot,
):
    repo = setup_for_generation(
        request.node.name,
        "https://hub.allspice.io/NoIndexTests/Altium-Hierarchical-Device-Sheet-Repetitions-Demo",
    )
    design_reuse_repo = setup_for_generation(
        request.node.name + "_reuse",
        "https://hub.allspice.io/NoIndexTests/Altium-Device-Sheets-Hierarchical-Repetitions",
    )

    instance.use_new_schdoc_renderer = False
    components = list_components_for_altium(
        instance,
        repo,
        "NestedDeviceSheets.PrjPcb",
        design_reuse_repos=[design_reuse_repo],
    )

    components.sort(key=lambda x: x["Designator"])
    assert len(components) == 980
    assert components == json_snapshot

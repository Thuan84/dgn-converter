"""
DGN to KML Converter API
FastAPI server that converts MicroStation DGN files to KML/GeoJSON
using GDAL/OGR library.

Deployed on Render.com (Free Tier).
"""

import os
import re
import uuid
import tempfile
import logging
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from osgeo import ogr, osr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="DGN Converter API",
    description="Convert MicroStation DGN files to KML/GeoJSON for web map viewing",
    version="1.0.0",
)

# CORS - allow frontend to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Max file size: 15MB
MAX_FILE_SIZE = 15 * 1024 * 1024


@app.get("/")
def health_check():
    """Health check endpoint - also keeps Render from sleeping."""
    return {"status": "ok", "service": "dgn-converter"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/drivers")
def list_drivers():
    """List all available OGR drivers — useful for debugging DGN support."""
    drivers = []
    for i in range(ogr.GetDriverCount()):
        drv = ogr.GetDriver(i)
        drivers.append(drv.GetName())
    dgn_support = {
        "DGN_V7": "DGN" in drivers,
        "DGN_V8": "DGNV8" in drivers,
    }
    return {"drivers": sorted(drivers), "dgn_support": dgn_support, "total": len(drivers)}


@app.post("/inspect")
async def inspect_dgn(file: UploadFile = File(...)):
    """Inspect DGN field names and sample values — for debugging label extraction."""
    content = await file.read()
    import tempfile, uuid, os
    tmp_dir = tempfile.mkdtemp()
    ext = os.path.splitext(file.filename or "test.dgn")[1].lower()
    tmp_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{ext}")
    with open(tmp_path, "wb") as f:
        f.write(content)

    result = {"layers": []}
    for drv_name in ["DGNV8", "DGN"]:
        drv = ogr.GetDriverByName(drv_name)
        if not drv:
            continue
        ds = drv.Open(tmp_path, 0)
        if not ds:
            continue

        result["driver_used"] = drv_name
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayer(i)
            if not lyr:
                continue
            defn = lyr.GetLayerDefn()
            fields = []
            for j in range(defn.GetFieldCount()):
                fd = defn.GetFieldDefn(j)
                fields.append({"name": fd.GetName(), "type": fd.GetTypeName()})

            # Sample first 10 POINT features
            samples = []
            lyr.ResetReading()
            feat = lyr.GetNextFeature()
            checked = 0
            while feat and checked < 200 and len(samples) < 10:
                checked += 1
                geom = feat.GetGeometryRef()
                if geom and geom.GetGeometryType() in (ogr.wkbPoint, ogr.wkbPoint25D):
                    row = {}
                    for j in range(defn.GetFieldCount()):
                        try:
                            row[defn.GetFieldDefn(j).GetName()] = feat.GetField(j)
                        except Exception:
                            row[defn.GetFieldDefn(j).GetName()] = None
                    samples.append(row)
                feat = lyr.GetNextFeature()

            result["layers"].append({
                "name": lyr.GetName(),
                "fields": fields,
                "point_samples": samples,
            })
        ds = None
        break

    try:
        os.remove(tmp_path)
        os.rmdir(tmp_dir)
    except Exception:
        pass

    return result




def detect_source_srs(datasource) -> osr.SpatialReference | None:
    """Try to detect the spatial reference from the DGN file."""
    layer = datasource.GetLayer(0)
    if layer is None:
        return None

    srs = layer.GetSpatialRef()
    if srs:
        return srs

    # If no SRS found, return None (caller will handle default)
    return None




def _postprocess_kml(kml_content: str) -> str:
    """Post-process KML to fix polygon styling and point labels from DGN conversion.

    - Remove opaque polygon fills (set to transparent)
    - Fix <name> for text-point Placemarks: extract from ExtendedData when name is a
      raw DGN type number (e.g. '17') or empty
    - Add default style with outline-only polygons
    """
    # Fix Point Placemark names: extract actual text label from ExtendedData
    # GDAL DGN driver stores the text in fields like EntityNum/Text/TextString
    # but KML <name> gets the element type code (e.g., '17' for text elements)
    kml_content = _fix_point_labels(kml_content)

    # Remove all existing PolyStyle blocks (they contain opaque fills from DGN)
    kml_content = re.sub(
        r'<PolyStyle>.*?</PolyStyle>',
        '<PolyStyle><fill>0</fill><outline>1</outline></PolyStyle>',
        kml_content,
        flags=re.DOTALL,
    )

    # If there are Style blocks with color fills, make polygon colors transparent
    kml_content = re.sub(
        r'(<PolyStyle>\s*<color>)[0-9a-fA-F]{8}(</color>)',
        r'\g<1>00000000\2',
        kml_content,
    )

    # Inject a default shared style at the top of Document for clean outlines
    default_style = """
    <Style id="dgn_outline">
      <LineStyle>
        <color>ff0000ff</color>
        <width>1.5</width>
      </LineStyle>
      <PolyStyle>
        <fill>0</fill>
        <outline>1</outline>
      </PolyStyle>
    </Style>"""

    kml_content = kml_content.replace('<Document>', f'<Document>{default_style}', 1)

    logger.info("KML post-processing: fixed labels + removed opaque fills + added outline style")
    return kml_content


def _fix_point_labels(kml_content: str) -> str:
    """Fix <name> for Point Placemarks that have a numeric-only or empty name.

    GDAL DGN → KML outputs element type code (17 = text element) as <name>.
    The actual text string is in <ExtendedData><SimpleData name="EntityNum"> or
    <SimpleData name="Text"> or <SimpleData name="TextString">.

    Strategy:
    1. Parse only Placemarks that contain <Point> geometry
    2. If their <name> is numeric-only (or empty): extract real text from ExtendedData
    3. If no real text found: remove the Placemark entirely (pure geometry elements)
    """
    from xml.etree import ElementTree as ET

    # Work on Placemark blocks individually via regex to avoid full XML parse overhead
    # Pattern: capture each <Placemark>...</Placemark> block
    placemark_re = re.compile(r'<Placemark\b[^>]*>.*?</Placemark>', re.DOTALL)

    # Field priority order for actual text label
    TEXT_FIELD_NAMES = ['EntityNum', 'Text', 'TextString', 'text', 'TEXT', 'Label',
                        'Description', 'Name']

    def _is_numeric_name(name: str) -> bool:
        """Return True if name is empty or just a number (likely a DGN type code)."""
        name = name.strip()
        return not name or name.isdigit()

    def _process_placemark(m: re.Match) -> str:
        block = m.group(0)

        # Only process if it contains a Point
        if '<Point>' not in block and '<Point ' not in block:
            return block  # Lines/polygons: keep as-is

        # Extract current <name>
        name_m = re.search(r'<name>(.*?)</name>', block, re.DOTALL)
        current_name = name_m.group(1).strip() if name_m else ''

        if not _is_numeric_name(current_name):
            # Name looks meaningful already (e.g., "CLN", "HNK 280") — keep it
            return block

        # Try <description> first (GDAL may put text label there)
        extracted_text = ''
        desc_m = re.search(r'<description>(.*?)</description>', block, re.DOTALL)
        if desc_m:
            desc_val = desc_m.group(1).strip()
            if desc_val and not desc_val.isdigit():
                extracted_text = desc_val
            elif desc_val and desc_val.isdigit() and len(desc_val) >= 3:
                extracted_text = desc_val

        # Try SimpleData fields in ExtendedData
        if not extracted_text:
            for field_name in TEXT_FIELD_NAMES:
                pat = rf'<SimpleData name="{re.escape(field_name)}">(.*?)</SimpleData>'
                sd_m = re.search(pat, block, re.DOTALL | re.IGNORECASE)
                if sd_m:
                    val = sd_m.group(1).strip()
                    if val and not val.isdigit():
                        extracted_text = val
                        break
                    elif val and val.isdigit() and len(val) >= 3:
                        extracted_text = val
                        break

        if not extracted_text:
            # No useful text found → remove this point entirely (it's a DGN non-text element)
            return ''

        # Replace or insert <name> with extracted text
        if name_m:
            block = block[:name_m.start()] + f'<name>{extracted_text}</name>' + block[name_m.end():]
        else:
            block = block.replace('<Placemark>', f'<Placemark><name>{extracted_text}</name>', 1)

        return block

    result = placemark_re.sub(_process_placemark, kml_content)
    return result




def _polygon_to_linestring(geom):
    """Convert a polygon geometry to its boundary linestring(s).

    This eliminates polygon fills entirely — only the outline remains.
    Works for both Polygon and MultiPolygon.
    """
    geom_type = geom.GetGeometryType()
    rings = []

    if geom_type in (ogr.wkbPolygon, ogr.wkbPolygon25D):
        for ring_idx in range(geom.GetGeometryCount()):
            ring = geom.GetGeometryRef(ring_idx)
            if ring and ring.GetPointCount() > 1:
                line = ogr.Geometry(ogr.wkbLineString)
                for pt_idx in range(ring.GetPointCount()):
                    line.AddPoint(*ring.GetPoint(pt_idx))
                rings.append(line)

    elif geom_type in (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D):
        for poly_idx in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(poly_idx)
            if poly:
                sub = _polygon_to_linestring(poly)
                if sub:
                    if sub.GetGeometryType() == ogr.wkbLineString:
                        rings.append(sub)
                    else:
                        for k in range(sub.GetGeometryCount()):
                            rings.append(sub.GetGeometryRef(k).Clone())

    if not rings:
        return None
    if len(rings) == 1:
        return rings[0]

    multi = ogr.Geometry(ogr.wkbMultiLineString)
    for r in rings:
        multi.AddGeometry(r)
    return multi


def convert_dgn_to_format(
    input_path: str,
    output_format: str = "KML",
    source_epsg: int | None = None,
    central_meridian: float | None = None,
) -> bytes:
    """
    Convert a DGN/DXF file to KML or GeoJSON using OGR.

    Args:
        input_path: Path to input file
        output_format: 'KML' or 'GeoJSON'
        source_epsg: EPSG code of source coordinate system
        central_meridian: Central meridian (lon_0) for VN2000 provincial system

    Returns:
        Converted file content as bytes
    """
    # Try to open with explicit drivers
    src_ds = None
    tried_drivers = []
    file_ext = os.path.splitext(input_path)[1].lower()

    if file_ext in ('.dgn',):
        # Check DGN file format by reading magic bytes
        file_format_info = ""
        try:
            with open(input_path, "rb") as f:
                header = f.read(16)
            if len(header) >= 4:
                # DGN V8 files start with OLE2 container: D0 CF 11 E0
                if header[:4] == b'\xd0\xcf\x11\xe0':
                    file_format_info = "DGN V8 (OLE2 container detected)"
                else:
                    file_format_info = f"DGN V7 (header: {header[:8].hex()})"
            logger.info(f"File format detection: {file_format_info}")
        except Exception as e:
            logger.warning(f"Could not read file header: {e}")

        # Try DGN V8 driver first, then V7
        for driver_name_try in ["DGNV8", "DGN"]:
            drv = ogr.GetDriverByName(driver_name_try)
            if drv is not None:
                tried_drivers.append(driver_name_try)
                src_ds = drv.Open(input_path, 0)
                if src_ds is not None:
                    logger.info(f"Opened with driver: {driver_name_try}")
                    break
    elif file_ext in ('.dxf',):
        drv = ogr.GetDriverByName("DXF")
        if drv:
            tried_drivers.append("DXF")
            src_ds = drv.Open(input_path, 0)
    elif file_ext in ('.dwg',):
        # Try CAD driver (uses OpenCAD library) and DWG driver
        for driver_name_try in ["CAD", "DWG"]:
            drv = ogr.GetDriverByName(driver_name_try)
            if drv is not None:
                tried_drivers.append(driver_name_try)
                src_ds = drv.Open(input_path, 0)
                if src_ds is not None:
                    logger.info(f"Opened DWG with driver: {driver_name_try}")
                    break

        # Fallback: use libredwg's dwg2dxf to convert DWG → DXF
        if src_ds is None:
            logger.info("GDAL CAD driver failed. Trying libredwg dwg2dxf conversion...")
            try:
                import subprocess
                dxf_path = input_path.replace('.dwg', '.dxf')
                result = subprocess.run(
                    ["dwg2dxf", "-y", "-o", dxf_path, input_path],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0 and os.path.isfile(dxf_path) and os.path.getsize(dxf_path) > 0:
                    logger.info(f"dwg2dxf converted: {dxf_path} ({os.path.getsize(dxf_path)} bytes)")
                    drv = ogr.GetDriverByName("DXF")
                    if drv:
                        src_ds = drv.Open(dxf_path, 0)
                        if src_ds:
                            tried_drivers.append("libredwg→DXF")
                            logger.info("Successfully opened libredwg-converted DXF")
                            input_path = dxf_path
                else:
                    logger.warning(f"dwg2dxf failed: {result.stderr[:500]}")
            except FileNotFoundError:
                logger.warning("dwg2dxf not found (libredwg-tools not installed)")
            except Exception as e:
                logger.warning(f"dwg2dxf conversion error: {e}")

    # Fallback: let OGR auto-detect
    if src_ds is None:
        tried_drivers.append("auto-detect")
        src_ds = ogr.Open(input_path, 0)

    if src_ds is None:
        available = ", ".join(tried_drivers)
        fmt_msg = f" Detected format: {file_format_info}." if file_ext == '.dgn' and file_format_info else ""

        if file_ext == '.dgn' and 'V8' in (file_format_info or ''):
            raise ValueError(
                f"File DGN V8 không được hỗ trợ trực tiếp. "
                f"Vui lòng mở file trong MicroStation → File → Save As → "
                f"chọn định dạng DXF hoặc DGN V7 → upload lại file mới."
            )
        raise ValueError(
            f"Cannot open file.{fmt_msg} Tried drivers: [{available}]."
        )

    layer_count = src_ds.GetLayerCount()
    if layer_count == 0:
        raise ValueError("DGN file contains no layers.")

    logger.info(f"DGN file has {layer_count} layer(s)")

    # Determine output driver and extension
    if output_format.upper() == "KML":
        driver_name = "KML"
        ext = ".kml"
    elif output_format.upper() == "GEOJSON":
        driver_name = "GeoJSON"
        ext = ".geojson"
    else:
        raise ValueError(f"Unsupported output format: {output_format}")

    out_driver = ogr.GetDriverByName(driver_name)
    if out_driver is None:
        raise RuntimeError(f"OGR driver '{driver_name}' not available")

    # Create output file
    base_name = os.path.splitext(input_path)[0]
    output_path = base_name + ext
    if os.path.exists(output_path):
        os.remove(output_path)

    out_ds = out_driver.CreateDataSource(output_path)
    if out_ds is None:
        raise RuntimeError("Failed to create output datasource")

    # Setup coordinate transformation
    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(4326)  # WGS84 (what KML/web maps use)
    target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    coord_transform = None
    if central_meridian:
        # Build VN2000 provincial projection from central meridian
        vn2000_proj = (
            f'+proj=tmerc +lat_0=0 +lon_0={central_meridian} +k=0.9999 '
            f'+x_0=500000 +y_0=0 +ellps=WGS84 '
            f'+towgs84=-191.90441429,-39.30318279,-111.45032835,'
            f'-0.00928836,0.01975479,-0.00427372,0.252906278 '
            f'+units=m +no_defs'
        )
        source_srs = osr.SpatialReference()
        source_srs.ImportFromProj4(vn2000_proj)
        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        coord_transform = osr.CoordinateTransformation(source_srs, target_srs)
        logger.info(f"Using VN2000 with central meridian: {central_meridian}")
    elif source_epsg:
        source_srs = osr.SpatialReference()
        source_srs.ImportFromEPSG(source_epsg)
        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        coord_transform = osr.CoordinateTransformation(source_srs, target_srs)
    else:
        # Try to detect from file
        detected_srs = detect_source_srs(src_ds)
        if detected_srs:
            detected_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            coord_transform = osr.CoordinateTransformation(detected_srs, target_srs)

    total_features = 0
    skipped = 0
    total_points = 0
    # Max point labels to avoid crashing browser (DGN cadastral files can have 50k+ points)
    MAX_POINT_LABELS = 3000

    # Polygon types that should be converted to linestrings (to avoid fills)
    POLYGON_TYPES = {
        ogr.wkbPolygon, ogr.wkbPolygon25D,
        ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D,
    }
    POINT_TYPES = {
        ogr.wkbPoint, ogr.wkbPoint25D,
        ogr.wkbMultiPoint, ogr.wkbMultiPoint25D,
    }

    # Common DGN text field names (GDAL exposes DGN text as these fields)
    # EntityNum is the GDAL DGN driver field that contains the actual text annotation
    TEXT_FIELDS = ['EntityNum', 'Text', 'TEXT', 'text', 'Label', 'LABEL', 'TextString',
                   'Feature_Code', 'Description', 'Name', 'NAME']

    for i in range(layer_count):
        src_layer = src_ds.GetLayer(i)
        if src_layer is None:
            continue

        layer_name = src_layer.GetName() or f"Layer_{i}"
        feature_count = src_layer.GetFeatureCount()
        logger.info(f"Processing layer '{layer_name}' with {feature_count} features")

        # Create output layer - force to generic geometry so mixed types work
        out_layer = out_ds.CreateLayer(
            layer_name,
            srs=target_srs,
            geom_type=ogr.wkbUnknown,
        )

        # Copy field definitions from source layer
        src_defn = src_layer.GetLayerDefn()
        for j in range(src_defn.GetFieldCount()):
            field_defn = src_defn.GetFieldDefn(j)
            out_layer.CreateField(field_defn)

        # Add a 'Name' field — GDAL KML driver maps the 'Name' field to <name>
        # We populate it with the actual text extracted from StyleString for point features
        name_field_already_exists = src_defn.GetFieldIndex('Name') >= 0
        if not name_field_already_exists:
            out_layer.CreateField(ogr.FieldDefn('Name', ogr.OFTString))
        out_defn = out_layer.GetLayerDefn()
        name_out_idx = out_defn.GetFieldIndex('Name')

        src_layer.ResetReading()
        feature = src_layer.GetNextFeature()
        while feature is not None:
            geom = feature.GetGeometryRef()
            current_text_label = ''  # reset each feature iteration
            if geom is not None:
                geom_type = geom.GetGeometryType()

                # For point features: only keep if there is meaningful text label
                if geom_type in POINT_TYPES:
                    if total_points >= MAX_POINT_LABELS:
                        feature = src_layer.GetNextFeature()
                        skipped += 1
                        continue

                    current_text_label = ''
                    src_defn_scan = src_layer.GetLayerDefn()

                    # PRIMARY: Extract from OGR StyleString
                    style_str = feature.GetStyleString() or ''
                    if style_str:
                        m = re.search(r'LABEL\([^)]*\bt:"([^"]*)"', style_str)
                        if not m:
                            m = re.search(r'LABEL\([^)]*\bt:([^,)]+)', style_str)
                        if m:
                            current_text_label = m.group(1).strip()

                    # FALLBACK: scan fields with GetFieldAsString()
                    if not current_text_label:
                        for tf in TEXT_FIELDS:
                            idx = src_defn_scan.GetFieldIndex(tf)
                            if idx >= 0:
                                try:
                                    val = feature.GetFieldAsString(idx).strip()
                                    if val and val != '0':
                                        current_text_label = val
                                        break
                                except Exception:
                                    pass

                    # LAST RESORT: scan all string-typed fields
                    if not current_text_label:
                        for j in range(feature.GetFieldCount()):
                            ft = src_defn_scan.GetFieldDefn(j).GetType()
                            if ft == ogr.OFTString:
                                try:
                                    val = feature.GetFieldAsString(j).strip()
                                    if val and val != '0' and not val.isdigit():
                                        current_text_label = val
                                        break
                                except Exception:
                                    pass

                    # Debug: log first 5 found points
                    if total_points < 5:
                        all_fields = {
                            f"{src_defn_scan.GetFieldDefn(j).GetName()}({src_defn_scan.GetFieldDefn(j).GetTypeName()})":
                            feature.GetFieldAsString(j)
                            for j in range(feature.GetFieldCount())
                        }
                        native = (feature.GetNativeData() or '')[:150]
                        logger.info(f"[DEBUG] Pt#{total_points}: style={style_str[:120]} | fields={all_fields} | native={native} → label={repr(current_text_label)}")

                    # Skip if no label
                    if not current_text_label or not current_text_label.strip():
                        feature = src_layer.GetNextFeature()
                        skipped += 1
                        continue

                    total_points += 1

                if coord_transform:
                    geom.Transform(coord_transform)

                # Convert polygons to their boundary linestrings (no fill!)
                if geom_type in POLYGON_TYPES:
                    boundary_geom = _polygon_to_linestring(geom)
                    if boundary_geom is None:
                        feature = src_layer.GetNextFeature()
                        continue
                    out_feature = ogr.Feature(out_layer.GetLayerDefn())
                    out_feature.SetGeometry(boundary_geom)
                else:
                    out_feature = ogr.Feature(out_layer.GetLayerDefn())
                    out_feature.SetGeometry(geom)

                # Copy field values from source feature
                for j in range(feature.GetFieldCount()):
                    try:
                        out_feature.SetField(j, feature.GetField(j))
                    except Exception:
                        pass

                # For point features: set the Name field to extracted text label
                # GDAL KML driver maps 'Name' field → <name> in each Placemark
                if geom_type in POINT_TYPES and name_out_idx >= 0 and current_text_label:
                    try:
                        out_feature.SetField(name_out_idx, current_text_label)
                    except Exception:
                        pass

                out_layer.CreateFeature(out_feature)
                total_features += 1

            feature = src_layer.GetNextFeature()

    # Cleanup OGR datasets
    out_ds = None
    src_ds = None

    logger.info(f"Converted {total_features} features ({total_points} point labels), skipped {skipped} features")

    if total_features == 0:
        raise ValueError("No geometry features found in DGN file.")

    # Read output and post-process KML to fix polygon styling
    with open(output_path, "r", encoding="utf-8") as f:
        content = f.read()

    if output_format.upper() == "KML":
        # Debug: log a sample of the raw KML to see how GDAL structured Point Placemarks
        import re as _re
        pm_matches = list(_re.finditer(r'<Placemark\b[^>]*>.*?</Placemark>', content, _re.DOTALL))
        point_samples = [m.group(0) for m in pm_matches if '<Point>' in m.group(0) or '<Point ' in m.group(0)]
        if point_samples:
            logger.info(f"[DEBUG] Raw KML - first Point Placemark sample:\n{point_samples[0][:600]}")
        else:
            logger.info("[DEBUG] No Point Placemarks found in raw KML output")

        content = _postprocess_kml(content)

    result = content.encode("utf-8")

    # Cleanup temp output
    try:
        os.remove(output_path)
    except Exception:
        pass

    return result


@app.post("/convert")
async def convert_dgn(
    file: UploadFile = File(..., description="DGN/DXF/DWG file to convert"),
    format: str = Query("KML", description="Output format: KML or GeoJSON"),
    source_epsg: int | None = Query(None, description="Source EPSG code (e.g., 9210 for VN2000 Mui 6)"),
    central_meridian: float | None = Query(None, description="Central meridian for VN2000 provincial system (e.g., 108.25 for Ninh Thuan)"),
):
    """
    Convert a DGN file to KML or GeoJSON.

    - Upload a .dgn file
    - Optionally specify the source coordinate system (EPSG code)
    - Returns the converted KML or GeoJSON file
    """
    # Validate file extension
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    allowed_ext = (".dgn", ".dxf", ".dwg")
    if not file.filename.lower().endswith(allowed_ext):
        raise HTTPException(400, f"Only {', '.join(allowed_ext)} files are accepted")

    # Read file content
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, f"File too large. Max size: {MAX_FILE_SIZE // (1024*1024)}MB")

    if len(content) == 0:
        raise HTTPException(400, "Empty file")

    # Save to temp file (keep original extension for driver auto-detection)
    file_ext = os.path.splitext(file.filename)[1].lower()
    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{file_ext}")

    try:
        with open(input_path, "wb") as f:
            f.write(content)

        logger.info(f"Converting {file.filename} ({len(content)} bytes) to {format}")

        result = convert_dgn_to_format(input_path, format, source_epsg, central_meridian)

        # Determine content type
        if format.upper() == "KML":
            media_type = "application/vnd.google-earth.kml+xml"
            out_ext = ".kml"
        else:
            media_type = "application/geo+json"
            out_ext = ".geojson"

        out_filename = file.filename.rsplit(".", 1)[0] + out_ext

        return Response(
            content=result,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{out_filename}"',
                "X-Original-Filename": file.filename,
                "X-Output-Format": format.upper(),
            },
        )

    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.error(f"Conversion failed: {e}", exc_info=True)
        raise HTTPException(500, f"Conversion failed: {str(e)}")
    finally:
        # Cleanup
        try:
            os.remove(input_path)
            os.rmdir(tmp_dir)
        except Exception:
            pass


# Common VN2000 EPSG codes for reference
VN2000_CODES = {
    "VN2000 / TM-3 zone 481 (Mui 3, KTT 104.75)": 9205,
    "VN2000 / TM-3 zone 482 (Mui 3, KTT 105.75)": 9206,
    "VN2000 / TM-3 zone 491 (Mui 3, KTT 106.25)": 9207,
    "VN2000 / UTM zone 48N (Mui 6)": 9209,
    "VN2000 / UTM zone 49N (Mui 6)": 9210,
}


@app.get("/epsg-codes")
def list_epsg_codes():
    """List common VN2000 EPSG codes for coordinate transformation."""
    return {"codes": VN2000_CODES}

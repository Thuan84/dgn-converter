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
    """Post-process KML to fix polygon styling from DGN conversion.

    - Remove opaque polygon fills (set to transparent)
    - Add default style with outline-only polygons
    - Clean up DGN-generated inline styles
    """
    # Remove all existing PolyStyle blocks (they contain opaque fills from DGN)
    kml_content = re.sub(
        r'<PolyStyle>.*?</PolyStyle>',
        '<PolyStyle><fill>0</fill><outline>1</outline></PolyStyle>',
        kml_content,
        flags=re.DOTALL,
    )

    # If there are Style blocks with color fills, make polygon colors transparent
    # KML color format: aabbggrr (alpha-blue-green-red)
    # Replace any polygon fill color with fully transparent
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

    # Insert after <Document> tag
    kml_content = kml_content.replace(
        '<Document>',
        f'<Document>{default_style}',
        1,
    )

    # Point Placemarks with just a name like "17" are DGN text annotations - remove them
    kml_content = re.sub(
        r'<Placemark>\s*<name>\d{1,4}</name>\s*<Style>.*?</Style>\s*'
        r'<Point>.*?</Point>\s*</Placemark>',
        '',
        kml_content,
        flags=re.DOTALL,
    )

    logger.info("KML post-processing: removed opaque fills, added outline style")
    return kml_content


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
    # Try to open DGN with explicit drivers
    src_ds = None
    tried_drivers = []

    # Try DGN V8 driver first (MicroStation V8+)
    for driver_name_try in ["DGNV8", "DGN"]:
        drv = ogr.GetDriverByName(driver_name_try)
        if drv is not None:
            tried_drivers.append(driver_name_try)
            src_ds = drv.Open(input_path, 0)
            if src_ds is not None:
                logger.info(f"Opened DGN with driver: {driver_name_try}")
                break

    # Fallback: let OGR auto-detect
    if src_ds is None:
        tried_drivers.append("auto-detect")
        src_ds = ogr.Open(input_path, 0)

    if src_ds is None:
        available = ", ".join(tried_drivers)
        raise ValueError(
            f"Cannot open DGN file. Tried drivers: [{available}]. "
            f"The file may be DGN V8 format (requires Teigha/ODA libraries) or corrupted."
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

    # Polygon types that should be converted to linestrings (to avoid fills)
    POLYGON_TYPES = {
        ogr.wkbPolygon, ogr.wkbPolygon25D,
        ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D,
    }
    POINT_TYPES = {
        ogr.wkbPoint, ogr.wkbPoint25D,
        ogr.wkbMultiPoint, ogr.wkbMultiPoint25D,
    }

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

        # Copy field definitions
        src_defn = src_layer.GetLayerDefn()
        for j in range(src_defn.GetFieldCount()):
            field_defn = src_defn.GetFieldDefn(j)
            out_layer.CreateField(field_defn)

        # Process features
        src_layer.ResetReading()
        feature = src_layer.GetNextFeature()
        while feature is not None:
            geom = feature.GetGeometryRef()
            if geom is not None:
                geom_type = geom.GetGeometryType()

                # Point features: keep only if they have meaningful text (not just numbers)
                if geom_type in POINT_TYPES:
                    text_val = feature.GetField("Text") if feature.GetFieldIndex("Text") >= 0 else None
                    if text_val is None:
                        text_val = feature.GetField("Name") if feature.GetFieldIndex("Name") >= 0 else None
                    # Skip if text is empty, purely numeric, or very short number
                    if not text_val or re.match(r'^\s*\d{1,5}\s*$', str(text_val)):
                        skipped += 1
                        feature = src_layer.GetNextFeature()
                        continue

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

                # Copy field values
                for j in range(feature.GetFieldCount()):
                    try:
                        out_feature.SetField(j, feature.GetField(j))
                    except Exception:
                        pass

                out_layer.CreateFeature(out_feature)
                total_features += 1

            feature = src_layer.GetNextFeature()

    # Cleanup OGR datasets
    out_ds = None
    src_ds = None

    logger.info(f"Converted {total_features} features, skipped {skipped} text/point annotations")

    if total_features == 0:
        raise ValueError("No geometry features found in DGN file.")

    # Read output and post-process KML to fix polygon styling
    with open(output_path, "r", encoding="utf-8") as f:
        content = f.read()

    if output_format.upper() == "KML":
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
    file: UploadFile = File(..., description="DGN/DXF file to convert"),
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

    allowed_ext = (".dgn", ".dxf")
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

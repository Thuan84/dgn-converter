"""
Direct DGN → GeoJSON converter (fast path).

Builds GeoJSON FeatureCollection in-memory from OGR features.
Eliminates KML file I/O, regex post-processing, and XML overhead.
~50-70% faster than the KML conversion path.
"""

import os
import re
import json
import logging

from osgeo import ogr, osr

logger = logging.getLogger(__name__)


def convert_dgn_direct_geojson(
    input_path: str,
    source_epsg: int | None = None,
    central_meridian: float | None = None,
    # Import helpers from main module at call time to avoid circular imports
    _helpers: dict | None = None,
) -> bytes:
    """Convert DGN/DXF/DWG to GeoJSON directly in-memory.

    This is the fast path: no intermediate KML file, no regex
    post-processing, no XML parsing. Features are built as Python
    dicts and serialized once with json.dumps().

    Args:
        input_path: Path to input .dgn/.dxf/.dwg file
        source_epsg: EPSG code of source coordinate system
        central_meridian: Central meridian for VN2000 provincial projection
        _helpers: Dict with helper functions from main module

    Returns:
        GeoJSON bytes (UTF-8 encoded)
    """
    # Resolve helpers
    h = _helpers or {}
    _fix_text_encoding = h.get('fix_text_encoding', lambda t, f='': t)
    _detect_font_from_style = h.get('detect_font_from_style', lambda s: '')
    _is_tcvn3_font = h.get('is_tcvn3_font', lambda f: False)
    _polygon_to_linestring = h.get('polygon_to_linestring')
    _cluster_text_points = h.get('cluster_text_points', lambda pts: pts)

    # ── Open file ────────────────────────────────────────────────
    src_ds = None
    tried_drivers = []
    file_ext = os.path.splitext(input_path)[1].lower()

    if file_ext == '.dgn':
        file_format_info = ""
        try:
            with open(input_path, "rb") as f:
                header = f.read(16)
            if len(header) >= 4:
                if header[:4] == b'\xd0\xcf\x11\xe0':
                    file_format_info = "DGN V8 (OLE2)"
                else:
                    file_format_info = "DGN V7"
            logger.info(f"File format: {file_format_info}")
        except Exception:
            file_format_info = ""

        for dn in ["DGNV8", "DGN"]:
            drv = ogr.GetDriverByName(dn)
            if drv:
                tried_drivers.append(dn)
                src_ds = drv.Open(input_path, 0)
                if src_ds:
                    logger.info(f"Opened with {dn}")
                    break

    elif file_ext == '.dxf':
        drv = ogr.GetDriverByName("DXF")
        if drv:
            tried_drivers.append("DXF")
            src_ds = drv.Open(input_path, 0)

    elif file_ext == '.dwg':
        for dn in ["CAD", "DWG"]:
            drv = ogr.GetDriverByName(dn)
            if drv:
                tried_drivers.append(dn)
                src_ds = drv.Open(input_path, 0)
                if src_ds:
                    break
        if src_ds is None:
            raise ValueError(
                "File DWG không được hỗ trợ. Vui lòng chuyển sang DXF."
            )

    # Fallback auto-detect
    if src_ds is None:
        tried_drivers.append("auto")
        src_ds = ogr.Open(input_path, 0)

    if src_ds is None:
        if file_ext == '.dgn' and 'V8' in (file_format_info or ''):
            raise ValueError(
                "File DGN V8 không được hỗ trợ. Vui lòng chuyển sang DXF hoặc DGN V7."
            )
        raise ValueError(
            f"Cannot open file. Tried: [{', '.join(tried_drivers)}]"
        )

    layer_count = src_ds.GetLayerCount()
    if layer_count == 0:
        raise ValueError("File contains no layers.")

    logger.info(f"File has {layer_count} layer(s)")

    # ── Coordinate transform ─────────────────────────────────────
    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(4326)
    target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    coord_transform = None
    if central_meridian:
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
        logger.info(f"VN2000 central meridian: {central_meridian}")
    elif source_epsg:
        source_srs = osr.SpatialReference()
        source_srs.ImportFromEPSG(source_epsg)
        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        coord_transform = osr.CoordinateTransformation(source_srs, target_srs)
    else:
        layer0 = src_ds.GetLayer(0)
        if layer0:
            detected = layer0.GetSpatialRef()
            if detected:
                detected.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                coord_transform = osr.CoordinateTransformation(detected, target_srs)
            else:
                # DGN/CAD files lack embedded SRS. Check if coordinates are projected VN-2000 meters (> 1000)
                try:
                    ext = layer0.GetExtent()  # (minX, maxX, minY, maxY)
                    if ext and max(abs(ext[0]), abs(ext[1]), abs(ext[2]), abs(ext[3])) > 1000:
                        northing = (ext[2] + ext[3]) / 2.0
                        if northing < 800000:
                            northing = (ext[0] + ext[1]) / 2.0
                        approx_lat = northing / 110574.0
                        if 11.0 <= approx_lat <= 12.5:
                            auto_ktt = 108.25
                        elif 8.5 <= approx_lat < 11.0:
                            auto_ktt = 105.75
                        elif 12.5 < approx_lat <= 16.5:
                            auto_ktt = 108.00
                        elif 16.5 < approx_lat <= 19.0:
                            auto_ktt = 106.50
                        elif 19.0 < approx_lat <= 23.5:
                            auto_ktt = 105.00
                        else:
                            auto_ktt = 108.25

                        vn2000_proj = (
                            f'+proj=tmerc +lat_0=0 +lon_0={auto_ktt} +k=0.9999 '
                            f'+x_0=500000 +y_0=0 +ellps=WGS84 '
                            f'+towgs84=-191.90441429,-39.30318279,-111.45032835,'
                            f'-0.00928836,0.01975479,-0.00427372,0.252906278 '
                            f'+units=m +no_defs'
                        )
                        source_srs = osr.SpatialReference()
                        source_srs.ImportFromProj4(vn2000_proj)
                        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                        coord_transform = osr.CoordinateTransformation(source_srs, target_srs)
                        logger.info(f"[AUTO-DETECT] Extent={ext} -> lat={approx_lat:.2f}° -> VN2000 KTT={auto_ktt}°")
                except Exception as ex:
                    logger.warning(f"Failed to auto-detect VN2000 projection: {ex}")

    # ── Constants ────────────────────────────────────────────────
    MAX_BUFFERED_POINTS = 50000
    MAX_POINT_LABELS = 25000
    POLYGON_TYPES = {
        ogr.wkbPolygon, ogr.wkbPolygon25D,
        ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D,
    }
    POINT_TYPES = {
        ogr.wkbPoint, ogr.wkbPoint25D,
        ogr.wkbMultiPoint, ogr.wkbMultiPoint25D,
    }
    TEXT_FIELDS = [
        'Text', 'TEXT', 'text', 'EntityNum', 'TextString',
        'Label', 'LABEL', 'Feature_Code', 'Description',
        'Name', 'NAME', 'SubClasses', 'RawCodeValues',
    ]
    SKIP_LEVELS_INT = {3, 13}
    SKIP_LAYER_NAMES = {'3', '13'}

    _is_dxf = file_ext == '.dxf'
    _label_regex_1 = re.compile(r'LABEL\([^)]*\bt:"([^"]*)"')
    _label_regex_2 = re.compile(r'LABEL\([^)]*\bt:([^,)]+)')
    _garbage_re = re.compile(
        r'(?:vndci|vndcc|vndce|vndscrng|Fundci|Thhndci|CGN\s*S)',
        re.IGNORECASE,
    )

    def _is_valid_label(text: str) -> bool:
        """Filter out garbage text — keep only valid cadastral labels."""
        t = text.strip()
        if not t:
            return False
        if len(t) > 120:
            return False
        if _garbage_re.search(t):
            return False
        # Too many dots/special chars → metadata gibberish
        special = sum(1 for c in t if c in '.,:;{}()[]|\\/"\'')
        if special > max(3, len(t) * 0.4):
            return False
        return True

    # ── Feature iteration ────────────────────────────────────────
    geojson_features: list[dict] = []
    total_features = 0
    total_points = 0
    total_buffered_points = 0
    skipped = 0
    level_skip_count = 0

    for i in range(layer_count):
        src_layer = src_ds.GetLayer(i)
        if src_layer is None:
            continue

        layer_name = src_layer.GetName() or f"Layer_{i}"
        logger.info(f"Layer '{layer_name}': {src_layer.GetFeatureCount()} features")

        src_defn = src_layer.GetLayerDefn()

        # Detect level/layer field index (cache per layer)
        level_idx = -1
        for lvl_name in ('Level', 'level', 'LEVEL', 'Layer', 'layer', 'LAYER'):
            level_idx = src_defn.GetFieldIndex(lvl_name)
            if level_idx >= 0:
                break

        text_point_buffer = []
        _font_logged = False

        src_layer.ResetReading()
        feature = src_layer.GetNextFeature()

        while feature is not None:
            geom = feature.GetGeometryRef()

            if geom is None:
                feature = src_layer.GetNextFeature()
                continue

            geom_type = geom.GetGeometryType()
            is_point = geom_type in POINT_TYPES

            # Level skip (non-point only)
            if level_idx >= 0 and not is_point:
                lv = feature.GetField(level_idx)
                if lv is not None:
                    should_skip = False
                    try:
                        lv_int = int(lv) if not isinstance(lv, int) else lv
                        if lv_int in SKIP_LEVELS_INT:
                            should_skip = True
                    except (ValueError, TypeError):
                        if str(lv).strip() in SKIP_LAYER_NAMES:
                            should_skip = True
                    if should_skip:
                        level_skip_count += 1
                        skipped += 1
                        feature = src_layer.GetNextFeature()
                        continue

            # ── Point features: extract text label ───────────────
            if is_point:
                if total_buffered_points >= MAX_BUFFERED_POINTS:
                    skipped += 1
                    feature = src_layer.GetNextFeature()
                    continue

                current_label = ''
                style_str = feature.GetStyleString() or ''
                detected_font = ''

                if style_str:
                    detected_font = _detect_font_from_style(style_str)
                    if detected_font and not _font_logged:
                        logger.info(f"[FONT] '{detected_font}' TCVN3={_is_tcvn3_font(detected_font)}")
                        _font_logged = True

                    m = _label_regex_1.search(style_str)
                    if not m:
                        m = _label_regex_2.search(style_str)
                    if m:
                        current_label = m.group(1).strip()

                # Fallback: scan fields
                if not current_label:
                    for tf in TEXT_FIELDS:
                        idx = src_defn.GetFieldIndex(tf)
                        if idx >= 0:
                            try:
                                val = feature.GetFieldAsString(idx).strip()
                                if val and val != '0':
                                    current_label = val
                                    break
                            except Exception:
                                pass

                current_label = _fix_text_encoding(current_label, detected_font)

                if not current_label or not current_label.strip() or not _is_valid_label(current_label):
                    skipped += 1
                    feature = src_layer.GetNextFeature()
                    continue

                total_buffered_points += 1

                # Buffer for clustering
                pt_geom = geom.Clone()
                if coord_transform:
                    pt_geom.Transform(coord_transform)
                pt_level = feature.GetField(level_idx) if level_idx >= 0 else None
                text_point_buffer.append({
                    'x': pt_geom.GetX(),
                    'y': pt_geom.GetY(),
                    'label': current_label,
                    'level': pt_level,
                })
                feature = src_layer.GetNextFeature()
                continue

            # ── Non-point features ───────────────────────────────
            # Transform in-place (safe — we read geom ref then move on)
            if coord_transform:
                geom.Transform(coord_transform)

            # DXF: extract text from non-point features
            if _is_dxf and total_buffered_points < MAX_BUFFERED_POINTS:
                dxf_text = ''
                dxf_style = feature.GetStyleString() or ''
                if dxf_style and 'LABEL' in dxf_style:
                    m = _label_regex_1.search(dxf_style)
                    if not m:
                        m = _label_regex_2.search(dxf_style)
                    if m:
                        dxf_text = m.group(1).strip()

                if not dxf_text:
                    for tf in ('Text', 'TEXT', 'text'):
                        tidx = src_defn.GetFieldIndex(tf)
                        if tidx >= 0:
                            try:
                                tv = feature.GetFieldAsString(tidx).strip()
                                if tv and tv != '0':
                                    dxf_text = tv
                                    break
                            except Exception:
                                pass

                if dxf_text:
                    dxf_font = _detect_font_from_style(dxf_style) if dxf_style else ''
                    dxf_text = _fix_text_encoding(dxf_text, dxf_font)
                    if dxf_text.strip() and _is_valid_label(dxf_text):
                        centroid = geom.Centroid()
                        if centroid:
                            dxf_level = feature.GetField(level_idx) if level_idx >= 0 else None
                            text_point_buffer.append({
                                'x': centroid.GetX(),
                                'y': centroid.GetY(),
                                'label': dxf_text,
                                'level': dxf_level,
                            })
                            total_buffered_points += 1

            # Convert polygon → linestring boundary (no fills)
            if geom_type in POLYGON_TYPES:
                boundary = _polygon_to_linestring(geom)
                if boundary is None:
                    feature = src_layer.GetNextFeature()
                    continue
                geojson_geom = json.loads(boundary.ExportToJson())
            else:
                geojson_geom = json.loads(geom.ExportToJson())

            geojson_features.append({
                'type': 'Feature',
                'geometry': geojson_geom,
                'properties': {},
            })
            total_features += 1

            feature = src_layer.GetNextFeature()

        # ── Cluster text points for this layer ───────────────────
        if text_point_buffer:
            clustered = _cluster_text_points(text_point_buffer)
            logger.info(
                f"Clustered {len(text_point_buffer)} text points → "
                f"{len(clustered)} merged labels"
            )
            for cp in clustered:
                if total_points >= MAX_POINT_LABELS:
                    skipped += 1
                    continue
                if not _is_valid_label(cp['label']):
                    skipped += 1
                    continue
                props = {'name': cp['label'], 'Name': cp['label']}
                if 'level' in cp and cp['level'] is not None:
                    props['level'] = cp['level']
                geojson_features.append({
                    'type': 'Feature',
                    'geometry': {
                        'type': 'Point',
                        'coordinates': [cp['x'], cp['y']],
                    },
                    'properties': props,
                })
                total_features += 1
                total_points += 1

    src_ds = None

    logger.info(
        f"Direct GeoJSON: {total_features} features "
        f"({total_points} labels), skipped {skipped} "
        f"(level-filtered: {level_skip_count})"
    )

    if total_features == 0:
        raise ValueError("No geometry features found in file.")

    result = {
        'type': 'FeatureCollection',
        'name': os.path.splitext(os.path.basename(input_path))[0],
        'features': geojson_features,
    }

    return json.dumps(result, ensure_ascii=False).encode('utf-8')

"""
MicroStation DGN V8 to DXF converter using Aspose.CAD.
Converts proprietary Bentley DGN V8 (OLE2) binary containers into DXF
so GDAL/OGR can parse layers, geometry, and Vietnamese text.
"""
import os
import logging

logger = logging.getLogger(__name__)


def is_dgn_v8_file(file_path: str) -> bool:
    """Check if file starts with Microsoft OLE2 magic bytes (DGN V8 signature)."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(16)
        return len(header) >= 4 and header[:4] == b'\xd0\xcf\x11\xe0'
    except Exception:
        return False


def convert_dgn_v8_to_dxf(input_path: str, output_dxf_path: str) -> bool:
    """
    Convert DGN V8 file to DXF using Aspose.CAD.
    Returns True if successful, False otherwise.
    """
    try:
        import aspose.cad as cad
        from aspose.cad.imageoptions import DxfOptions

        logger.info(f"[DGN V8 Auto-Convert] Converting {input_path} -> {output_dxf_path} via Aspose.CAD...")
        with cad.Image.load(input_path) as image:
            options = DxfOptions()
            image.save(output_dxf_path, options)

        if os.path.exists(output_dxf_path) and os.path.getsize(output_dxf_path) > 0:
            logger.info(f"[DGN V8 Auto-Convert] Succeeded! DXF size: {os.path.getsize(output_dxf_path)} bytes")
            return True
        else:
            logger.error(f"[DGN V8 Auto-Convert] Output file empty or missing: {output_dxf_path}")
            return False
    except ImportError:
        logger.warning("[DGN V8 Auto-Convert] aspose-cad library not installed.")
        return False
    except Exception as e:
        logger.error(f"[DGN V8 Auto-Convert] Aspose.CAD conversion error: {e}", exc_info=True)
        return False

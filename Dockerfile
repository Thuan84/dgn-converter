# Use FULL GDAL image for maximum format support (DGN V7, DGN V8, DXF, DWG...)
FROM ghcr.io/osgeo/gdal:ubuntu-full-3.9.3

# Install Python and pip
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy app code
COPY main.py .

# Expose port
EXPOSE 10000

# Run with uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]

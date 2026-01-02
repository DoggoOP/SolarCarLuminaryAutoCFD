# AutoCFD Solar Car Pipeline

A fully automated CFD pipeline for solar car aerodynamics testing. Upload a CAD file, get drag/lift/side force results automatically logged to Google Sheets, and deploy to the web in minutes.

## 🚀 Features

- **🌐 Web Interface** - Simple FastAPI dashboard for uploading CAD files and monitoring jobs
- **☁️ Luminary Cloud Integration** - Automated geometry processing, meshing, and RANS CFD simulation
- **📊 Google Sheets Logging** - Automatic results tracking with drag, lift, side force, and coefficients
- **🔄 Backfill Support** - Import historical simulation data into spreadsheets
- **⚙️ Smart Automation** - Automatic frontal area calculation, force outputs, and convergence monitoring
- **🔄 Rotating Wheels** - Optional rotating wheel simulation with auto-detection and customizable parameters
- **🚢 Deploy Ready** - Containerized and configured for Railway, Render, or Google Cloud Run

---

## 📋 Table of Contents

- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Google Sheets Integration](#google-sheets-integration)
- [Backfill Historical Results](#backfill-historical-results)
- [Deployment](#deployment)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)

---

## 🔧 How It Works

The pipeline automates the complete CFD workflow:

1. **Upload CAD** - Accepts STEP, STL, or other CAD formats via web interface
2. **Create Geometry** - Imports CAD into Luminary Cloud and computes bounding box
3. **Build Farfield** - Automatically creates rectangular farfield domain:
   - Width/Length: 25× vehicle dimensions (configurable)
   - Floor: 1mm below lowest point
   - Height: Scaled proportionally
4. **Calculate Reference Values**:
   - **Length**: Fixed at 5.8 meters (vehicle length)
   - **Area**: YZ projection (frontal area) calculated from bounding box
   - **Velocity**: User-specified wind speed (default 24.59 m/s)
5. **Generate Mesh** - Creates volume mesh with adaptive refinement (target: 10M cells)
6. **Setup Simulation**:
   - RANS turbulence (k-omega SST with γ-Reθ transition model)
   - Adaptive boundary layer (40 layers, 1.15 growth rate)
   - Moving floor boundary condition (constant ground speed)
   - Optional rotating wheels (auto-detected, 110.2 rad/s)
   - Stopping conditions: 7500 iterations or convergence
   - Force outputs: Drag, Lift, Side Force on body surfaces
7. **Run Simulation** - Launches and monitors CFD solve
8. **Extract Results** - Fetches force values and calculates coefficients:
   - Cd = Drag / (0.5 × ρ × V² × A)
   - Cl = Lift / (0.5 × ρ × V² × A)
   - Cs = Side Force / (0.5 × ρ × V² × A)
9. **Log to Sheets** - Appends results with timestamp, clickable Luminary link, and metadata

All status updates stream to the web dashboard in real-time.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- [Luminary Cloud account](https://luminarycloud.com) with API key
- (Optional) Google service account for Sheets integration

### Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd autoCFD

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and add your LUMINARY_API_KEY
```

### Run Locally

```bash
uvicorn app.main:app --reload
```

Visit **http://localhost:8000** to access the dashboard.

---

## 📊 Google Sheets Integration

### Setup

1. **Create Google Service Account**
   - Go to [Google Cloud Console](https://console.cloud.google.com)
   - Create new project or select existing
   - Enable Google Sheets API and Google Drive API
   - Create Service Account credentials
   - Download JSON key file

2. **Create Spreadsheet**
   - Create a new Google Sheet
   - Share it with your service account email (from JSON file)
   - Give "Editor" permissions
   - Copy the Spreadsheet ID from URL

3. **Configure Environment**
   ```bash
   # Add to .env
   GOOGLE_SHEETS_CREDENTIALS=/path/to/credentials.json
   GOOGLE_SHEETS_SPREADSHEET_ID=your_spreadsheet_id
   ```

4. **Automatic Headers**
   - Headers are created automatically on first run
   - Columns: Timestamp, Job Name, Simulation ID, Forces, Coefficients, Wind Speed, Frontal Area, Luminary Link

### What Gets Logged

Each simulation appends a row with:
- **Timestamp** - UTC time of completion
- **Job Name** - Simulation identifier
- **Simulation ID** - Luminary Cloud ID
- **Drag Force (N)** - Total drag force
- **Drag Coefficient** - Cd (dimensionless)
- **Lift Force (N)** - Total lift force
- **Lift Coefficient** - Cl (dimensionless)
- **Side Force (N)** - Total side force
- **Side Force Coefficient** - Cs (dimensionless)
- **Convergence Status** - COMPLETED, FAILED, etc.
- **Max Iterations** - 7500
- **Wind Speed (m/s)** - Reference velocity
- **Frontal Area (m²)** - Calculated YZ projection
- **Luminary Link** - Clickable URL to view results

See [GOOGLE_SHEETS_SETUP.md](GOOGLE_SHEETS_SETUP.md) for detailed setup instructions.

---

## 🔄 Backfill Historical Results

Import results from previously completed simulations:

```bash
# Dry run - see what would be imported
python -m app.backfill_sheets --dry-run

# Import all completed simulations
python -m app.backfill_sheets

# Import specific project
python -m app.backfill_sheets --project "AutoCFD Solar Car"

# Limit number of simulations
python -m app.backfill_sheets --limit 10
```

The backfill script:
- ✅ Fetches completed simulations from Luminary Cloud
- ✅ Extracts reference values (velocity, area) from simulation parameters
- ✅ Downloads force output data (drag, lift, side force)
- ✅ Calculates coefficients
- ✅ Appends to Google Sheets with all metadata

---

## 🚢 Deployment

Deploy your application to the cloud for 24/7 access.

### Quick Deploy to Railway (Recommended)

1. **Prepare credentials**:
   ```bash
   ./scripts/prepare_credentials_for_deployment.sh
   ```

2. **Push to GitHub**:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```

3. **Deploy on Railway**:
   - Go to [railway.app](https://railway.app)
   - Sign up with GitHub
   - Click "New Project" → "Deploy from GitHub repo"
   - Select your repository
   - Add environment variables (see below)
   - Railway auto-deploys!

4. **Set Environment Variables** in Railway dashboard:
   ```
   LUMINARY_API_KEY=<your_api_key>
   LUMINARY_PROJECT_NAME=AutoCFD Solar Car
   DEFAULT_FARFIELD_SPEED=24.59
   GOOGLE_SHEETS_SPREADSHEET_ID=<your_spreadsheet_id>
   GOOGLE_SHEETS_CREDENTIALS=<json_string_from_script>
   ```

Your app will be live at: `https://your-app.railway.app`

### Other Deployment Options

- **Render** - See [QUICKSTART_DEPLOY.md](QUICKSTART_DEPLOY.md)
- **Google Cloud Run** - See [DEPLOYMENT.md](DEPLOYMENT.md)
- **Docker** - `docker build -t autocfd . && docker run -p 8000:8000 autocfd`

**Deployment Documentation:**
- 📖 [QUICKSTART_DEPLOY.md](QUICKSTART_DEPLOY.md) - 10-minute deployment guide
- 📖 [DEPLOYMENT.md](DEPLOYMENT.md) - Comprehensive deployment options
- 🐳 [Dockerfile](Dockerfile) - Container configuration

---

## ⚙️ Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LUMINARY_API_KEY` | ✅ Yes | - | Your Luminary Cloud API token |
| `LUMINARY_PROJECT_NAME` | No | `AutoCFD Solar Car` | Project name for simulations |
| `DEFAULT_FARFIELD_SPEED` | No | `24.59` | Default wind speed (m/s) |
| `BASE_SIM_TEMPLATE_PATH` | No | `data/base_simulation_params.json` | Simulation template |
| `SPEED_OF_SOUND` | No | `340.29` | Speed of sound (m/s) for Mach calculation |
| `UPLOADS_DIR` | No | `uploads` | Directory for temporary CAD files |
| `GOOGLE_SHEETS_CREDENTIALS` | No | - | Path to JSON or JSON string |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | No | - | Google Sheet ID for logging |

### Simulation Template

Edit `data/base_simulation_params.json` to customize:

- **Turbulence Model** - Currently k-omega SST with QCR correction
- **Solver Settings** - CFL=50, NODAL_GRADIENT, preconditioning enabled
- **Convergence Criteria** - Set via API (7500 max iterations)
- **Boundary Conditions**:
  - Body surfaces: No-slip wall
  - Floor: Moving wall (velocity = freestream)
  - Farfield: Pressure farfield

**Note**: `convergenceCriteria` must be set via API, not in JSON template.

### Reference Values

- **Length**: Always 5.8 meters (vehicle length)
- **Area**: Automatically calculated as YZ projection (frontal area)
- **Velocity**: User-specified wind speed from form

### Force Outputs

Automatically configured for every simulation:
- **Drag** - Force in X direction (body frame)
- **Lift** - Force in Z direction (body frame)
- **Side Force** - Force in Y direction (body frame)

All forces integrate over body surfaces (excludes floor and farfield).

---

## 📡 API Reference

### Web Interface

- **GET /** - Dashboard with job list and upload form
- **POST /run** - Submit new simulation job
- **GET /jobs** - List all jobs
- **GET /jobs/{job_id}** - Get specific job status

### Submit Simulation

```bash
curl -F cad_file=@solar_car.step \
     -F cad_label="TestRun" \
     -F project_name="AutoCFD Solar Car" \
     -F farfield_speed=24.59 \
     -F mesh_min_size=0.002 \
     -F mesh_max_size=0.05 \
     -F farfield_multiplier=25.0 \
     -F rotating_wheels=true \
     http://localhost:8000/run
```

**Response:**
```json
{
  "job_id": "abc123"
}
```

### Check Job Status

```bash
curl http://localhost:8000/jobs/abc123
```

**Response:**
```json
{
  "job_id": "abc123",
  "name": "AutoCFD run for TestRun",
  "status": "running",
  "logs": [
    "Uploaded CAD to uploads/xyz.step",
    "Geometry created with id=geom-123...",
    "Mesh generation in progress..."
  ],
  "result": null,
  "error": null,
  "created_at": "2025-12-19T19:30:00Z"
}
```

---

## 🔍 Troubleshooting

### Common Issues

**Problem**: "Google Sheets is not configured"
- **Solution**: Set `GOOGLE_SHEETS_CREDENTIALS` and `GOOGLE_SHEETS_SPREADSHEET_ID` in `.env`
- Verify credentials file exists or JSON string is valid

**Problem**: "Could not fetch forces: 'Simulation' object has no attribute 'list_output_values'"
- **Solution**: This was fixed in the latest version. Make sure you're using the updated code.

**Problem**: "Mesh generation failed"
- **Solution**: Check CAD file quality. Ensure geometry is watertight and properly scaled.
- Try increasing `mesh_max_size` or decreasing `mesh_min_size`

**Problem**: "Simulation terminated with status FAILED"
- **Solution**: Check Luminary Cloud logs at the URL provided in job output
- Verify boundary conditions and physics settings in template

**Problem**: Deployment fails with port error
- **Solution**: Make sure you're using the latest Dockerfile that properly reads `$PORT`
- Railway/Render set `$PORT` dynamically - don't hardcode it

### Getting Help

1. **Check Logs**:
   - Local: Terminal output from `uvicorn`
   - Deployed: Platform logs (Railway/Render dashboard)

2. **Luminary Cloud Logs**:
   - Click the simulation link in job output
   - View detailed solver logs and convergence history

3. **Google Sheets Issues**:
   - Verify service account has Editor permissions on spreadsheet
   - Check credentials JSON is valid
   - See [GOOGLE_SHEETS_SETUP.md](GOOGLE_SHEETS_SETUP.md)

4. **File Issues**:
   - Check logs in deployment platform
   - Use GitHub Issues for bug reports

---

## 📁 Project Structure

```
autoCFD/
├── app/
│   ├── main.py                 # FastAPI application
│   ├── config.py               # Settings and environment variables
│   ├── luminary_pipeline.py    # Core CFD automation logic
│   ├── sheets_logger.py        # Google Sheets integration
│   ├── backfill_sheets.py      # Historical data import
│   ├── job_store.py            # In-memory job tracking
│   └── templates/
│       └── index.html          # Web dashboard
├── data/
│   └── base_simulation_params.json  # CFD template
├── scripts/
│   └── prepare_credentials_for_deployment.sh
├── .env.example                # Environment template
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Container configuration
├── railway.json                # Railway deployment config
├── render.yaml                 # Render deployment config
├── DEPLOYMENT.md               # Full deployment guide
├── QUICKSTART_DEPLOY.md        # Quick deployment guide
├── GOOGLE_SHEETS_SETUP.md      # Sheets setup guide
└── README.md                   # This file
```

---

## 🛠️ Advanced Usage

### Custom Surface Mapping

If your CAD uses non-standard boundary names:

```bash
curl -F cad_file=@car.step \
     -F cad_label="Custom" \
     -F body_surfaces="body,shell,panel" \
     -F floor_surfaces="ground,road" \
     -F farfield_surfaces="domain,outer" \
     http://localhost:8000/run
```

### Adjust Farfield Domain

```bash
# Larger farfield (50x instead of 25x)
-F farfield_multiplier=50.0

# Add padding (meters)
-F farfield_padding=5.0

# Override center point (x,y,z)
-F farfield_center="0,0,1.5"
```

### Custom Wind Direction

```bash
# Wind from side (45° angle)
-F wind_direction="0.707,0.707,0"

# Wind from behind
-F wind_direction="-1,0,0"
```

### Rotating Wheels Configuration

Enable rotating wheel simulation with automatic surface detection:

```bash
# Enable rotating wheels (auto-detect)
-F rotating_wheels=true
```

**How Auto-Detection Works:**
- Detects surfaces in the wheel contact zone (z ∈ [0.0, 0.065] meters)
- Floor is at z = -0.01m (bbox minimum - 0.001m)
- Categorizes wheels into front/rear based on X-coordinate
- Front wheels: higher X values (more positive)
- Rear wheels: lower X values (closer to rear)

**Manual Surface Override:**

Specify exact wheel surface names if auto-detection fails:

```bash
-F rotating_wheels=true \
-F wheel_surfaces="0/bound/BC_5,0/bound/BC_6,0/bound/BC_7,0/bound/BC_8"
```

**Wheel Motion Parameters:**
- **Rotation Rate**: 110.2 rad/s around Y-axis (lateral rotation)
- **Front Wheel Center**: (x=0, y=0, z=0.28) in global coordinates
- **Rear Wheel Center**: (x=-2.679, y=0, z=0.28) in global coordinates
- **Motion Formulation**: MRF (Moving Reference Frame) for steady-state
- **Boundary Condition**: NO_SLIP on wheel surfaces

**What Happens:**
1. Wheel surfaces are automatically detected or use manual override
2. Two rotating motion frames are created (front_wheels_frame, rear_wheels_frame)
3. Wheels get separate boundary condition (excluded from car body BC)
4. Simulation includes wheel rotation effects on aerodynamics

**Example Log Output:**
```
Rotating wheels enabled - detecting wheel surfaces...
Detected 4 wheel surfaces: ['0/bound/BC_5', '0/bound/BC_6', '0/bound/BC_7', '0/bound/BC_8']
Front wheels: ['0/bound/BC_5', '0/bound/BC_6'], Rear: ['0/bound/BC_7', '0/bound/BC_8']
```

---

## 📈 Performance Notes

- **Mesh Generation**: ~5-15 minutes depending on geometry complexity
- **Simulation Runtime**: ~30-60 minutes for typical solar car (7500 iterations)
- **Adaptive Refinement**: Targets 10M cells with automatic refinement
- **Parallel Processing**: 2 concurrent jobs supported (configurable in `main.py`)

---

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

---

## 📄 License

This project uses the Luminary Cloud SDK and is subject to Luminary Cloud's terms of service.

---

## 🔗 Resources

- [Luminary Cloud API Documentation](https://app.luminarycloud.com/docs/api/reference)
- [FastAPI Documentation](https://fastapi.tiangolo.com)
- [Google Sheets API](https://developers.google.com/sheets/api)
- [Railway Deployment](https://docs.railway.app)
- [Render Deployment](https://render.com/docs)

---

## 🙏 Acknowledgments

Built with:
- [Luminary Cloud](https://luminarycloud.com) - CFD simulation platform
- [FastAPI](https://fastapi.tiangolo.com) - Web framework
- [gspread](https://github.com/burnash/gspread) - Google Sheets integration

---

**Questions?** Check the deployment guides or open an issue on GitHub.

**Ready to deploy?** → [QUICKSTART_DEPLOY.md](QUICKSTART_DEPLOY.md)

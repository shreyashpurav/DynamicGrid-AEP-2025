# AEP Dynamic Grid Analyzer

AEP Dynamic Grid Analyzer is an interactive, web-based simulator for power grid stress analysis, cascading failure modeling, and AI-driven remediation. It combines a Python/Flask backend, a physics simulation engine, and a genetic algorithm with a dynamic JavaScript and Leaflet.js frontend.

## Key Features

* **Dynamic Physics Simulation**: Interactively adjust **Temperature**, **Wind Speed**, and **Load Multiplier** to see their real-time impact on grid stability. The backend uses the `ieee738` standard to calculate dynamic thermal ratings for conductors.
* **Interactive Map Visualization**: A [Leaflet.js](https://leafletjs.com/) map displays all grid buses and transmission lines.
    * Lines are color-coded in real-time (OK, Compromised, Overloaded, Offline) based on simulation results.
    * All lines are clickable to manually toggle on/off status, simulating outages for N-1 analysis.
* **AI-Powered Remediation**: Utilizes a **Genetic Algorithm** (using the `deap` library) to find an optimal, low-cost remediation plan for an overloaded grid.
    * The AI suggests a plan of **Strategic Reroutes** (taking lines offline) and **Load Curtailment** (reducing overall load).
    * The fitness function minimizes a weighted score of `(failure_count * 1,000,000) + total_cost` to find the most effective and economical solution.
    * The suggested plan can be applied to the grid simulation with a single click.
* **Cascading Failure Analysis**: Run a multi-round simulation to visualize how initial failures can propagate and cascade through the grid. The map updates with each round to show the failure progression.
* **N-1 Contingency Analysis**: Runs a simulation for every possible single-line failure (N-1) to identify which line outage causes the most subsequent failures, identifying the grid's most critical components.
* **Breakpoint Analysis**: Automatically finds the precise Temperature or Wind Speed "breakpoint" at which the current grid configuration will begin to experience overloads.
* **24-Hour & 7-Day Forecasting**:
    * **24h Sim**: Run an animated simulation of a full day, using a sinusoidal load curve to model typical daily usage.
    * **7-Day Forecast**: Pulls a real-world 7-day weather forecast from the Open-Meteo API and predicts the number of failures and system stress for each day.

## Technology Stack

| Category | Technology | Description |
| :--- | :--- | :--- |
| **Backend** | Python 3 | Core programming language. |
| | Flask | Web server for hosting the API and frontend. |
| | Pandas | Used for all data loading, merging, and manipulation of grid data. |
| **Core Engine** | ieee738 | A Python library used for calculating the steady-state thermal rating of conductors. |
| | DEAP | A (Distributed Evolutionary Algorithms in Python) library used for the Genetic Algorithm remediation engine. |
| **Frontend** | Vanilla JavaScript (ES6+) | Handles all client-side logic, UI events, and API calls (`fetch`). |
| | Leaflet.js | An open-source JavaScript library for interactive maps. |
| | TailwindCSS | A utility-first CSS framework for styling the user interface. |

## How It Works

### 1. Physics Engine

The core of the simulation is the `calculate_physics_state_internal` function in `app.py`.
1.  It takes `temp`, `wind`, `load_mult`, and a set of `forced_offline_lines` as input.
2.  It uses the `ieee738` library to calculate the dynamic thermal `rating_mva` (ampacity) for each *active* line based on the weather.
3.  It calculates the `current_load_mva` for each line, which includes a redistribution formula. Load from offline lines is redistributed proportionally among the remaining active lines at the same voltage level.
4.  It compares `current_load_mva` to `rating_mva` to determine line status (`overloaded`, `stressed`, `ok`) and bus status.
5.  This state is returned as a JSON object to the frontend.

### 2. AI Remediation Engine

When the "Find Remediation Plan" button is clicked, the `/api/find-remediation` endpoint is called.
1.  A "baseline" simulation is run to get the current number of failures.
2.  The `deap` Genetic Algorithm toolbox is initialized. An "individual" in the population is a list of `RemedialAction` objects (either `REROUTE` or `CURTAIL`).
3.  The GA runs for 30 generations, trying to find an individual (a plan) that minimizes the fitness function: `(failure_count * 1,000,000) + total_cost`. This heavily prioritizes solving failures while also seeking a low-cost plan.
4.  The best plan found is returned to the user, who can then click "Apply This Remediation Plan".
5.  Applying the plan triggers the `applyRemediationPlan` function, which makes API calls to `toggle-line` for reroutes and updates the load slider for curtailment, then refreshes the dashboard.

## Setup and Installation

**Note:** This project requires specific data files that are **not** included in this repository. You must provide them in the correct directory structure.

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/your-username/aep-dynamic-grid-analyzer.git
    cd aep-dynamic-grid-analyzer
    ```

2.  **Create and Activate a Virtual Environment(optional)**
    ```bash
    # (macOS / Linux)
    python3 -m venv venv
    source venv/bin/activate

    # (Windows)
    python -m venv venv
    .\venv\Scripts\activate
    ```

3.  **Install Python Dependencies**
    ```bash
    pip install -r requirements.txt
    ```
4.  **Run using your python installation**
    ```bash
    python app.py
    ```
    The server will start, load the data, and become available at `http://127.0.0.1:5000/`.

5.  **Open the Application**
    Open `index.html` in your web browser, or navigate to `http://127.0.0.1:5000/`.

## API Endpoints

The Flask server (`app.py`) provides the following API endpoints:

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Serves the main `index.html` file. |
| `GET` | `/static/<path:path>` | Serves static files (JS, CSS). |
| `GET` | `/api/map-buses` | Returns JSON of all bus coordinates for Leaflet. |
| `GET` | `/api/map-lines` | Returns GeoJSON of all transmission lines for Leaflet. |
| `POST` | `/api/toggle-line` | Toggles a line's status between 'online' and 'offline' in the global server state. |
| `GET` | `/api/grid-status` | **Core Endpoint.** Runs a full simulation based on `temp`, `wind`, and `load_mult` query params using the global offline state. |
| `GET` | `/api/n-1-analysis` | Runs an N-1 contingency analysis for the current grid state. |
| `POST` | `/api/cascade-round` | Runs a *single round* of the cascade simulation for a *provided* grid state (does not use global state). |
| `POST` | `/api/find-remediation`| Runs the Genetic Algorithm to find a remediation plan for the *provided* grid state. |
| `POST` | `/api/predict` | Runs the simulation for each day in a 7-day forecast payload. |

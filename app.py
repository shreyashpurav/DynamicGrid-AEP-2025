from flask import Flask, jsonify, request, json
import random
from deap import base, creator, tools, algorithms
from functools import partial
from dataclasses import dataclass
import pandas as pd
import numpy as np
from ieee738.ieee738 import ConductorParams, Conductor

app = Flask(__name__)

# Global data storage
master_df = None
bus_coords_df = None
# Track which lines are offline
offline_lines = set()  

#Constants for initialization
TEMP_INIT = 25.0
WIND_INIT = 2.0
LOAD_INIT = 1.0

# --- Remediation Engine Constants ---
#Arbitrarily assigned costs, no data to support
COST_REROUTE_LINE = 500
COST_LOAD_CURTAIL = 2000 # Per 0.05 step
LOAD_CURTAIL_STEP = 0.05
MAX_ACTIONS_PER_SOLUTION = 5 # Keep solutions simple
ALL_LINE_NAMES = [] # Will be populated in load_master_data

@dataclass
class RemedialAction:
    action_type: str # Will either be 'REROUTE' or 'CURTAIL'
    value: str or float

    def __repr__(self):
        if self.action_type == 'REROUTE':
            return f"Strategic Reroute: {self.value}"
        if self.action_type == 'CURTAIL':
            return f"Load Curtail: -{self.value * 100:.0f}%"
    
    def to_dict(self):
        return {'desc': str(self), 'type': self.action_type, 'value': self.value}

# --- Genetic Algorithm Fitness Function ---
def evaluate_remediation(individual, baseline_temp, baseline_wind, baseline_load, baseline_offline_set):
    """
    DEAP Fitness Function.
    Calculates the fitness of a single 'individual' (a list of RemedialActions).
    Score = (Failures * 1,000,000) + TotalCost
    We want to MINIMIZE this score.
    """
    global master_df
    
    current_cost = 0
    sim_load_mult = baseline_load
    sim_offline_set = set(baseline_offline_set) # Start with a copy of current offline lines

    # Calculate cost of actions
    for action in individual:
        if action.action_type == 'REROUTE':
            # Only add cost if it's a new action, not redundant
            if action.value not in sim_offline_set:
                current_cost += COST_REROUTE_LINE
                sim_offline_set.add(action.value)
        elif action.action_type == 'CURTAIL':
            current_cost += COST_LOAD_CURTAIL
            sim_load_mult -= action.value # value is the step, e.g. 0.05
    #Test the solutions effectiveness
    sim_results = calculate_physics_state_internal(
        baseline_temp, 
        baseline_wind, 
        sim_load_mult, 
        forced_offline_lines=sim_offline_set
    )
    
    failure_count = 0
    if sim_results:
        failure_count = sim_results['report']['failure_count']
    else:
        failure_count = 999 # Code to handle if the simulation does not work

    # Calculate final weighted score
    return (failure_count * 1000000) + current_cost,

# --- GA Toolbox Setup ---
creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
creator.create("Individual", list, fitness=creator.FitnessMin)

toolbox = base.Toolbox()

def create_random_action():
    # Gene factory: Creates one random RemedialAction
    if random.random() < 0.75: # 75% chance of reroute
        return RemedialAction(action_type='REROUTE', value=random.choice(ALL_LINE_NAMES))
    else: # 25% chance of load curtail
        return RemedialAction(action_type='CURTAIL', value=LOAD_CURTAIL_STEP)

def mutReplaceAction(individual, indpb):
    """
    Mutation operator: for each action in the individual, 
    with probability indpb, replace it with a new random action.
    """
    for i in range(len(individual)):
        if random.random() < indpb:
            individual[i] = create_random_action()
    return individual,

toolbox.register("attr_action", create_random_action)
toolbox.register("individual", tools.initRepeat, creator.Individual, toolbox.attr_action, n=random.randint(1, MAX_ACTIONS_PER_SOLUTION))
toolbox.register("population", tools.initRepeat, list, toolbox.individual)

toolbox.register("mate", tools.cxOnePoint)
toolbox.register("mutate", mutReplaceAction, indpb=0.2)
toolbox.register("select", tools.selTournament, tournsize=3)


def load_master_data():
    #Load and merge all CSV data on startup
    global master_df, bus_coords_df, ALL_LINE_NAMES
    
    try:
        #Reads in all the data
        lines_df = pd.read_csv('hawaii40_osu/csv/lines.csv')
        buses_df = pd.read_csv('hawaii40_osu/csv/buses.csv')
        line_flows_df = pd.read_csv('hawaii40_osu/line_flows_nominal.csv')
        conductor_df = pd.read_csv('ieee738/conductor_library.csv')
        #Merges data into one master file for ease of use
        master_df = lines_df.merge(
            buses_df[['name', 'v_nom']], 
            left_on='bus0', 
            right_on='name', 
            how='left',
            suffixes=('', '_bus')
        )
        #Ignores bus
        master_df = master_df.drop(columns=['name_bus'])
        
        master_df = master_df.merge(
            line_flows_df[['name', 'p0_nominal']], 
            on='name', 
            how='left'
        )
        
        master_df = master_df.merge(
            conductor_df[['ConductorName', 'RES_25C', 'RES_50C', 'CDRAD_in']], 
            left_on='conductor', 
            right_on='ConductorName'
        )
        
        bus_coords_df = buses_df[['name', 'x', 'y']].copy()
        
        if master_df is not None:
            ALL_LINE_NAMES = master_df['name'].tolist()
        
        print("--- Master Data Loaded Successfully ---")
        print(master_df.info())
        
    except FileNotFoundError as e:
        print(f"FATAL ERROR: Could not find data file: {e}")
    except KeyError as e:
        print(f"FATAL ERROR: Data column mismatch: {e}. Check CSV column names.")
    except Exception as e:
        print(f"FATAL ERROR during data load: {e}")

def calculate_physics_state(temp, wind, load_mult):
    #Kept here so we did not have to go through and change every method name
    return calculate_physics_state_internal(temp, wind, load_mult, forced_offline_lines=None)
        
def calculate_physics_state_internal(temp, wind, load_mult, forced_offline_lines=None):
    """
    Runs the full physics simulation for all lines and buses.
    It can be run in two modes:
    1. forced_offline_lines=None (default): Uses the global 'offline_lines' set.
    2. forced_offline_lines=set([...]): Uses the provided set for the simulation,
       leaving the global 'offline_lines' set untouched.
    """
    global master_df, offline_lines
    if master_df is None:
        return None

    # Use the forced set if provided, otherwise fall back to the global session lines
    current_offline_set = forced_offline_lines if forced_offline_lines is not None else offline_lines

    line_results = []
    bus_status = {}


    # Separate lines into active and offline using the determined set
    active_lines_df = master_df[~master_df['name'].isin(current_offline_set)]
    offline_lines_df = master_df[master_df['name'].isin(current_offline_set)]

    # Calculate the total load from offline lines per voltage level
    offline_load_mva_by_vnom = (
        offline_lines_df.groupby('v_nom')['p0_nominal'].sum() * load_mult
    )
    
    # Calculate the total nominal load of active lines per voltage level
    active_nominal_load_by_vnom = active_lines_df.groupby('v_nom')['p0_nominal'].sum()

    # Iterate through all lines to simulate complete circuit
    for line in master_df.itertuples():
        
        # Handle offline lines using the determined set
        if line.name in current_offline_set:
            line_results.append({
                'name': line.name,
                'bus0': line.bus0,
                'bus1': line.bus1,
                'loading': 0,
                'initial_status': 'offline',
                'final_color': 'black',
                'current_mva': 0,
                'rating_mva': 0
            })
            continue

        # B. Handle active lines
        try:
            # Standard physics calculation for line rating
            res_lo_ft = line.RES_25C / 5280
            res_hi_ft = line.RES_50C / 5280
            diameter_ft = (line.CDRAD_in * 2) 
            acsr_params = {
                'TLo': 25, 'THi': 50,
                'RLo': res_lo_ft, 'RHi': res_hi_ft,
                'Diameter': diameter_ft, 'Tc': line.MOT
            }
            ambient_params = {
                'Ta': temp, 'WindVelocity': wind, 'WindAngleDeg': 90,
                'SunTime': 12, 'Date': '12 Jun', 'Emissivity': 0.8,
                'Absorptivity': 0.8, 'Direction': 'EastWest', 'Atmosphere': 'Clear',
                'Elevation': 1000, 'Latitude': 27,
            }
            cp = ConductorParams(**ambient_params, **acsr_params)
            con = Conductor(cp)
            rating_amps = con.steady_state_thermal_rating()
            V = line.v_nom * 1000
            rating_mva = (3**0.5) * rating_amps * V * 1e-6

            #REDISTRIBUTED LOAD CALCULATION as shown in pypsa github
            base_load_mva = line.p0_nominal * load_mult
            offline_load_at_v = offline_load_mva_by_vnom.get(line.v_nom, 0)
            total_active_load_at_v = active_nominal_load_by_vnom.get(line.v_nom, 0)
            extra_load_mva = 0
            if total_active_load_at_v > 0:
                redistribution_factor = line.p0_nominal / total_active_load_at_v
                extra_load_mva = offline_load_at_v * redistribution_factor
            current_load_mva = base_load_mva + extra_load_mva

            loading_percent = 0
            if rating_mva > 0:
                loading_percent = ((current_load_mva / rating_mva) * 100) - 100
            
            line_status = 'ok'
            if loading_percent > 0:
                line_status = 'overloaded'
            elif loading_percent > -15:
                line_status = 'stressed'

            line_results.append({
                'name': line.name,
                'bus0': line.bus0,
                'bus1': line.bus1,
                'loading': loading_percent,
                'initial_status': line_status,
                'final_color': 'green',
                'current_mva': current_load_mva,
                'rating_mva': rating_mva
            })

            if line_status == 'overloaded':
                bus_status[line.bus0] = 'overloaded'
                bus_status[line.bus1] = 'overloaded'
                
        except Exception as e:
            print(f"Error calculating line {line.name}: {e}")
            line_results.append({
                'name': line.name, 'bus0': line.bus0, 'bus1': line.bus1,
                'loading': 0, 'initial_status': 'error', 'final_color': 'gray',
                'current_mva': 0,
                'rating_mva': 0
            })

    # Processing the data from simulation
    total_stress_score = 0
    all_failures = []
    
    for line in line_results:
        if line['initial_status'] == 'offline':
            continue
        if line['initial_status'] == 'overloaded':
            line['final_color'] = 'red'
            total_stress_score += line['loading']
            all_failures.append(line)
        #checks if it is connected to any overloaded lines, therefor critical condition
        elif bus_status.get(line['bus0']) == 'overloaded' or \
             bus_status.get(line['bus1']) == 'overloaded':
            line['final_color'] = 'orange'
        else:
            line['final_color'] = 'green'

    active_lines_count = len(active_lines_df)
    #calculates average stress score
    if active_lines_count > 0:
        total_stress_score /= active_lines_count
    #sorts failures    
    all_failures.sort(key=lambda x: x['loading'], reverse=True)

    report_data = {
        'overall_stress': total_stress_score,
        'top_failures': all_failures,
        'failure_count': len(all_failures)
    }
        
    return {
        'line_results': line_results,
        'bus_results': bus_status,
        'report': report_data
    }    
@app.route('/api/map-buses')
def get_map_buses():
    #Serves the bus coordinate data as JSON
    global bus_coords_df
    if bus_coords_df is None:
        return jsonify({"error": "Bus data not loaded"}), 500
    return jsonify(bus_coords_df.to_dict(orient='records'))

@app.route('/api/map-lines')
def get_map_lines():
    #Serves the GeoJSON line data
    try:
        with open('hawaii40_osu/gis/oneline_lines.geojson', 'r') as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        print(f"Error loading GeoJSON: {e}")
        return jsonify({"error": "Could not load line geometry"}), 500

@app.route('/api/toggle-line', methods=['POST'])
def toggle_line():
    #Toggle a line online/offline
    global offline_lines
    
    data = request.json
    line_name = data.get('line_name')
    
    if not line_name:
        return jsonify({"error": "line_name required"}), 400
    
    if line_name in offline_lines:
        offline_lines.remove(line_name)
        new_status = 'online'
    else:
        offline_lines.add(line_name)
        new_status = 'offline'
    
    return jsonify({
        'line_name': line_name,
        'status': new_status,
        'offline_lines': list(offline_lines)
    })

@app.route('/api/grid-status')
def grid_status():
    #Main endpoint for real-time simulation
    global master_df 
    try:
        temp = request.args.get('temp', TEMP_INIT, type=float)
        wind = request.args.get('wind', WIND_INIT, type=float)
        load_mult = request.args.get('load_mult', LOAD_INIT, type=float)
        n = request.args.get('n', 5, type=int)
    except:
        return jsonify({"error": "Invalid input parameters"}), 400
        
    # Calls the wrapper function which uses the global offline_lines state
    results = calculate_physics_state(temp, wind, load_mult) 
    
    if results is None:
        return jsonify({"error": "Server data not loaded"}), 500
    #formats grid status to be returned as JSON
    response = {
        'lines': {
            line['name']: {
                'status_color': line['final_color'],
                'loading': round(line['loading'], 2),
                'current_mva': round(line.get('current_mva', 0), 2),
                'rating_mva': round(line.get('rating_mva', 0), 2)
            } for line in results['line_results']
        },
        'buses': results['bus_results'],
        'report': {
            'overall_stress': round(results['report']['overall_stress'], 2),
            'top_n_failures': [
                {'name': line['name'], 'loading': round(line['loading'], 2)}
                for line in results['report']['top_failures'][:n]
            ],
            'failure_count': results['report']['failure_count'],
            'total_lines': len(master_df) if master_df is not None else 0
        },
        'offline_lines': list(offline_lines) # Return the global state
    }
    
    return jsonify(response)

@app.route('/api/predict', methods=['POST'])
def predict():
    #Runs physics calculations on the 7 day forcast
    forecast_data = request.json
    predictions = []
    
    for day_data in forecast_data:
        try:
            temp = float(day_data['temp'])
            wind = float(day_data['wind'])
            load_mult = 1.15 #Uses max expected load of the day
        
            # Calls the wrapper, which uses the global offline_lines state
            results = calculate_physics_state(temp, wind, load_mult)
            
            failure_count = sum(
                1 for line in results['line_results'] 
                if line['loading'] > 0 and line['initial_status'] != 'offline'
            )
            stress_score = results['report']['overall_stress']
            
            predictions.append({
                'day': day_data['day'],
                'predicted_failures': failure_count,
                'predicted_stress': round(stress_score, 2)
            })
        except Exception as e:
            print(f"Error in forecast prediction: {e}")
            predictions.append({
                'day': day_data['day'],
                'predicted_failures': 'Error',
                'predicted_stress': 'Error'
            })
    
    return jsonify(predictions)

@app.route('/api/n-1-analysis')
def n_1_analysis():
    """
    Runs an N-1 contingency analysis to find the worst line failures.
    this function does not change the current state of the grid
    """
    global master_df, offline_lines
    if master_df is None:
        return jsonify({"error": "Server data not loaded"}), 500

    try:
        temp = request.args.get('temp', TEMP_INIT, type=float)
        wind = request.args.get('wind', WIND_INIT, type=float)
        load_mult = request.args.get('load_mult', LOAD_INIT, type=float)
    except:
        return jsonify({"error": "Invalid input parameters"}), 400

    # Store the original state of offline lines
    original_offline_lines = set(offline_lines)

    # Run simulation to get the baseline, passing the original state
    baseline_results = calculate_physics_state_internal(
        temp, wind, load_mult, 
        forced_offline_lines=original_offline_lines
    )
    if baseline_results is None:
        return jsonify({"error": "Baseline calculation failed"}), 500
    baseline_failure_count = baseline_results['report']['failure_count']
    
    contingency_results = []
    all_line_names = master_df['name'].tolist()
    
    # Iterate through every single line
    for line_to_fail in all_line_names:
        
        if line_to_fail in original_offline_lines:
            continue

        # Create the new test state
        contingency_offline_set = original_offline_lines.copy()
        contingency_offline_set.add(line_to_fail)
        
        # Run the simulation passing the test state
        contingency_sim_results = calculate_physics_state_internal(
            temp, wind, load_mult, 
            forced_offline_lines=contingency_offline_set
        )
        #report results of sim
        if contingency_sim_results:
            contingency_failure_count = contingency_sim_results['report']['failure_count']
            new_failures_caused = contingency_failure_count - baseline_failure_count
            contingency_results.append({
                'line_name': line_to_fail,
                'failures_caused': max(0, new_failures_caused) 
            })
            
    # Sort to find the worst contingencies
    contingency_results.sort(key=lambda x: x['failures_caused'], reverse=True)
    #return top 5 most volatile lines
    return jsonify(contingency_results[:5])

@app.route('/api/cascade-round', methods=['POST'])
def cascade_round():
    #Runs cascade analysis without changing the state of the circuit
    global master_df
    try:
        data = request.json
        temp = float(data.get('temp', TEMP_INIT))
        wind = float(data.get('wind', WIND_INIT))
        load_mult = float(data.get('load_mult', LOAD_INIT))
        n = int(data.get('n', 999)) # Get all failures for analysis
        
        # Get the offline lines
        sim_offline_lines = set(data.get('current_offline_lines', []))
        
    except Exception as e:
         print(f"Error parsing cascade-round JSON: {e}")
         return jsonify({"error": "Invalid input parameters"}), 400

    # Run the simulation using the state of the grid
    results = calculate_physics_state_internal(
        temp, wind, load_mult, 
        forced_offline_lines=sim_offline_lines
    )
    if results is None:
        return jsonify({"error": "Server data not loaded"}), 500
    
    # Return the same data structure as /api/grid-status
    response = {
        'lines': {
            line['name']: {
                'status_color': line['final_color'],
                'loading': round(line['loading'], 2),
                'current_mva': round(line.get('current_mva', 0), 2),
                'rating_mva': round(line.get('rating_mva', 0), 2)
            } for line in results['line_results']
        },
        'buses': results['bus_results'],
        'report': {
            'overall_stress': round(results['report']['overall_stress'], 2),
            # Return all failures so the frontend can figure the difference out in theory
            'top_n_failures': [ 
                {'name': line['name'], 'loading': round(line['loading'], 2)}
                for line in results['report']['top_failures'][:n] # n is 999
            ],
            'failure_count': results['report']['failure_count'],
            'total_lines': len(master_df) if master_df is not None else 0
        },
        # Return the offline lines that were passed in so the grid goes back to original state
        'offline_lines': list(sim_offline_lines) 
    }
    return jsonify(response)
    
@app.route('/api/find-remediation', methods=['POST'])
def find_remediation():
    """
    Runs the GA to find a minimal-cost remediation plan
    for a given failing grid state.
    """
    try:
        data = request.json
        temp = float(data.get('temp', TEMP_INIT))
        wind = float(data.get('wind', WIND_INIT))
        load_mult = float(data.get('load_mult', LOAD_INIT))
        baseline_offline_lines = set(data.get('baseline_offline_lines', []))
    except Exception as e:
         print(f"Error parsing remediation JSON: {e}")
         return jsonify({"error": "Invalid input parameters"}), 400

    # Run baseline simulation to see if we even need to run this thing
    baseline_results = calculate_physics_state_internal(
        temp, wind, load_mult, 
        forced_offline_lines=baseline_offline_lines
    )
    baseline_failures = baseline_results['report']['failure_count']
    
    if baseline_failures == 0: #no need to run the whole algorithm stuff
        return jsonify({
            'baseline_failures': 0,
            'remediated_failures': 0,
            'cost': 0,
            'plan': [{'desc': 'No failures detected. No action required.', 'type': 'INFO', 'value': ''}]
        })

    # provide the baseline state to our evaluation function
    toolbox.register("evaluate", partial(evaluate_remediation, 
                                     baseline_temp=temp, 
                                     baseline_wind=wind, 
                                     baseline_load=load_mult, 
                                     baseline_offline_set=baseline_offline_lines))
    
    # Run the Genetic Algorithm
    pop = toolbox.population(n=50)
    hof = tools.HallOfFame(1)
    
    # Run the algorithm
    # cxpb = crossover prob, mutpb = mutation prob, ngen = generations
    algorithms.eaSimple(pop, toolbox, cxpb=0.6, mutpb=0.2, ngen=30, 
                        halloffame=hof, verbose=False)
    
    # Extract the best solution
    best_individual = hof[0]
    best_fitness_score = best_individual.fitness.values[0]
    
    # De-duplicate the plan (GA might add L-10 twice for some reason that is not apparent due to sleep deprivation)
    final_plan_actions = []
    seen_reroutes = set()
    total_curtail = 0
    
    for action in best_individual:
        if action.action_type == 'REROUTE':
            if action.value not in baseline_offline_lines and action.value not in seen_reroutes:
                final_plan_actions.append(action)
                seen_reroutes.add(action.value)
        elif action.action_type == 'CURTAIL':
            total_curtail += action.value

    # total reduction of load
    if total_curtail > 0:
        final_plan_actions.append(RemedialAction(action_type='CURTAIL', value=total_curtail))
        
    # cool and fancy format
    remediated_failures = best_fitness_score // 1000000
    final_cost = best_fitness_score % 1000000
    
    return jsonify({
        'baseline_failures': baseline_failures,
        'remediated_failures': int(remediated_failures),
        'cost': int(final_cost),
        'plan': [a.to_dict() for a in final_plan_actions]
    })

@app.route('/')
def index():
    #Serve the main HTML page
    try:
        with open('index.html', 'r') as f:
            return f.read()
    except FileNotFoundError:
        return "index.html not found", 404

if __name__ == '__main__':
    load_master_data()
    app.run(debug=True)

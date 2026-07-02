import os
import requests
from google.adk.agents import Agent

def detect_grafana_dashboards() -> str:
    """Detects and returns a list of Grafana dashboards on localhost:3000."""
    try:
        grafana_user = os.environ.get('GRAFANA_USERNAME', 'admin')
        grafana_pass = os.environ.get('GRAFANA_PASSWORD', 'admin')
        response = requests.get(
            'http://localhost:3000/api/search?type=dash-db',
            auth=(grafana_user, grafana_pass)
        )
        response.raise_for_status()
        dashboards = response.json()
        if not dashboards:
            return "No dashboards found."
        
        result = "Found the following dashboards:\n"
        for dash in dashboards:
            title = dash.get('title', 'Unknown')
            url = dash.get('url', 'Unknown')
            uid = dash.get('uid', 'Unknown')
            result += f"- {title} (UID: {uid}, URL: {url})\n"
        return result
    except Exception as e:
        return f"Error detecting Grafana dashboards: {str(e)}"

def get_grafana_dashboard_details(uid: str) -> str:
    """Fetches the JSON configuration and panels for a specific Grafana dashboard by its UID."""
    try:
        grafana_user = os.environ.get('GRAFANA_USERNAME', 'admin')
        grafana_pass = os.environ.get('GRAFANA_PASSWORD', 'admin')
        response = requests.get(
            f'http://localhost:3000/api/dashboards/uid/{uid}',
            auth=(grafana_user, grafana_pass)
        )
        response.raise_for_status()
        dashboard_data = response.json()
        # Return only the panels part to avoid too large context if necessary, but returning the whole dashboard works
        return str(dashboard_data.get('dashboard', {}).get('panels', dashboard_data))
    except Exception as e:
        return f"Error fetching dashboard details: {str(e)}"

root_agent = Agent(
    model='gemini-3.5-flash',
    name='root_agent',
    description='A helpful assistant for user questions.',
    instruction='Answer user questions to the best of your knowledge. Use the detect_grafana_dashboards tool when asked about grafana dashboards, and use get_grafana_dashboard_details to fetch the actual data and metrics inside a specific dashboard using its UID. Do NOT use LaTeX math formatting (like $$) for formulas; write them in plain text instead.',
    tools=[detect_grafana_dashboards, get_grafana_dashboard_details],
)

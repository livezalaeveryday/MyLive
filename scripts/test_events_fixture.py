"""Real authenticated local backend for QA; connectors cannot be started."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from lca import create_app
class NoConnectors:
 def create(self,*args,**kwargs):raise RuntimeError('Platform connections disabled in QA')
app=create_app({'LOCAL_AUTH_TOKEN':'mylive-local-browser-qa-2026','BIND_HOST':'127.0.0.1'},connector_factory=NoConnectors())
if __name__=='__main__':
 print('QA http://127.0.0.1:8767/#token=mylive-local-browser-qa-2026&session=qa_test_events_2026',flush=True)
 app.extensions['socketio'].run(app,host='127.0.0.1',port=8767,allow_unsafe_werkzeug=True)

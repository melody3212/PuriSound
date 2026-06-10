import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
os.chdir(r'C:\Users\1524\Desktop\LG PuriSound\LG PuriSound\YAMNET')
sys.path.insert(0, r'C:\Users\1524\Desktop\LG PuriSound\LG PuriSound\YAMNET')
import app
app.app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)

import os
import sys

os.environ['DJANGO_SETTINGS_MODULE'] = 'powerdb2.settings'
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import django.core.handlers.wsgi
application = django.core.handlers.wsgi.WSGIHandler()

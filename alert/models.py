
import uuid
import time
import datetime
import operator
import httplib2

from django.db import models, transaction
from django.db.models import signals
from django.contrib.auth.models import User, Group
from django.template import Template, Context
try:
    from twisted.internet import reactor
    no_twisted = False
except NameError:
    no_twisted = True

from powerdb2 import settings
from smap.archiver.client import SmapClient

import emaillib

class Level(models.Model):
    """Alert levels in the future might control who receives a message"""
    description = models.CharField(max_length=16)
    priority = models.IntegerField()

    def __cmp__(self, other):
        print "CMO"
        return self.priority == other.priority

    def __gt__(self, other):
        return self.priority > other.priority

    def __lt__(self, other):
        return self.priority < other.priority

    def __unicode__(self):
        return self.description

def get_default_level():
    levels = Level.objects.filter(priority=30)
    if len(levels):
        return levels[0]
    else:
        return None

class Action(models.Model):
    """What to do when an alert gets triggered"""
    # when we last did something
    name = models.CharField(max_length=64, unique=True)
    rate = models.IntegerField(default=60, help_text="""
Number of seconds between checking the alert condition""")

    alert_when_true = models.BooleanField(default=True)
    alert_when_false = models.BooleanField(default=True)

    template = models.TextField(blank=True)

    def __unicode__(self):
        return self.name

    def send_alert(self, alert, setting_uuids, unsetting_uuids, 
                   users=[]):        
        # look up the tags for these streams
        uuids = set(setting_uuids.keys() + unsetting_uuids.keys())
        uuids = map(lambda u: "uuid = '%s'" % u, uuids)
        client = SmapClient()
        tags = client.tags(' or '.join(uuids), nest=True)
        tags = dict(((x['uuid'], x) for x in tags))

        def make_context(params):
            rv = []
            for uid, (t, v) in params.iteritems():
                if uid in tags:
                    rv.append(tags[uid])
                    rv[-1]['AlertTime'] = time.ctime(t/1000)
                    rv[-1]['AlertValue'] = v
            return rv
        setting, clearing = make_context(setting_uuids), make_context(unsetting_uuids)
        logentry = Log(alert=alert, 
                       when=datetime.datetime.now())
        logentry.save()

        # generate the text to send, by building a context for our
        # template.
        template = Template(self.template)
        context = Context({
                'setting' :  setting,
                'clearing' : clearing,
                'when_true' : self.alert_when_true,
                'when_false' : self.alert_when_false,
                'permalink' : settings.ROOT_NETLOC + '/admin/alert/log/' + str(logentry.id),
                'alarmpage' : settings.ROOT_NETLOC + '/admin/alert/alert/' + str(alert.id),
                'timestamp' : logentry.when,
                'alarm' : alert.__unicode__(),
                })
        logentry.message = template.render(context)
        logentry.save()

        emaillib.send(to, 'Alert from %s' % settings.ROOT_NETLOC, logentry.message)

def get_default_action():
    poss = Action.objects.filter(name="Default")
    if len(poss) > 0:
        return poss[0]
    else:
        return None

class Recipients(models.Model):
    description = models.CharField(max_length=64)
    users = models.ManyToManyField(User, blank=True, null=True)
    groups = models.ManyToManyField(Group, blank=True, null=True)
    extra_users = models.TextField(blank=True, help_text="""
Comma-separated list of additional email addresses to notify""")

    def emails(self):
        """Get a list of email addresses to notify"""
        
        to = [map(operator.attrgetter('email'), g.user_set.all()) for 
              g in self.groups.all()]
        to += [x.email for x in self.users.all()]
        to += self.extra_users.split(',')
        to = filter(None, to)

        return to

    def __unicode__(self):
        return self.description

class Alert(models.Model):
    """Represent an alert instance"""
    owner = models.ForeignKey(User)
    description = models.CharField(max_length=64, help_text="""
A human-friendly description of this alert""")
    grouping = models.CharField(max_length=64, blank=True, help_text="""
What group this alert is part of (like the building name)
""")
    key = models.CharField(max_length=512, help_text="""
Something to group alerts by -- you can search by key""")
    select = models.CharField(max_length=512, help_text="""
The 'where' clause of a query specifying the streams to run on.""")
    notification_frequency = models.IntegerField(default=3600,
                                                 help_text="""
The minimum time interval between notification messages for the same priority level.""")

    enabled = models.BooleanField(default=True)

    last_check = models.DateTimeField(null=True, blank=True, help_text="""
The last time the backend processed this alert definition.""")

    current_level = models.ForeignKey(Level, 
                                      default=lambda: Level.objects.get(priority=0))
    last_change = models.DateTimeField(null=True, blank=True, help_text="""
The last time the alert level changed""")

    last_notification = models.DateTimeField(null=True, help_text="""
The last time this alert sent a message""")

    last_priority = models.ForeignKey(Level, null=True, 
                                      related_name='last_priority_related',
                                      help_text="""
The priority level of the last alert we sent a message about""")
                                      

    # if we encountered an error creating the alert
    error = models.TextField(help_text="""
The error encountered.""")
    error_state = models.BooleanField(default=False, help_text="""
Weather the alert is currently active, with no problems.""")
    error_time = models.DateTimeField(null=True, blank=True, help_text="""
When we encountered this error.""")

    def __unicode__(self):
        return '(' + self.select + ") " 

    def current(self):
        c = SmapClient()
        latest = c.latest(self.select, streamlimit=1000)
        test = self.get_test()
        for v in latest:
            if len(v['Readings']):
                level = test(v['Readings'][0][1])
                v['level'] = {
                    "priority": level.priority,
                    "description": level.description,
                    }
        return latest

    def get_test(self):
        checks = self.checks.all().reverse().order_by('level__priority')
        tests = map(operator.methodcaller('get_test'), checks)
        levels = list(map(operator.attrgetter('level'), checks))
        unset_level = Level.objects.get(id=1)
        def testfn(x):
            for i in xrange(0, len(tests)):
                if tests[i](x):
                    return levels[i]
            return unset_level
        return testfn

comparators = { 
    'LT' : operator.__lt__,
    'GT' : operator.__gt__,
    'EQ' : operator.__eq__,
    'NEQ' : operator.__ne__,
    'LTE' : operator.__le__,
    'GTE' : operator.__ge__,
    }

TEST_COMPARATORS = (
    ('GT', u'>'),
    ('LT', u'<'),
    ('LTE', u'\u2264'),
    ('GTE', u'\u2265'),
    ('EQ', u'='),
    ('NEQ', u'\u2260'),
    )

class Check(models.Model):
    """A test condition to run on a model"""
    alert = models.ForeignKey(Alert, related_name='checks')
    level = models.ForeignKey(Level, default=get_default_level, 
                              help_text="""
How serious this alert is""")

    action = models.ForeignKey(Action, default=get_default_action)
    recipients = models.ForeignKey(Recipients, 
                                   null=True, blank=True,
                                   help_text="""
Who to notify when this alert changes state""")

    set = models.NullBooleanField(default=None, null=True, help_text="""
Weather this alert is currently 'set'.""")
    set_time = models.DateTimeField(null=True, blank=True, help_text="""
The last time the alert was set.""")
    clear_time = models.DateTimeField(null=True, blank=True, help_text="""
The last time the alert was cleared.""")

    value_1 = models.FloatField()
    comparator_1 = models.CharField(max_length=6, 
                                    choices=TEST_COMPARATORS, 
                                    default='GTE')

    value_2 = models.FloatField(null=True, blank=True)
    comparator_2 = models.CharField(max_length=6, 
                                    choices=TEST_COMPARATORS, 
                                    default=None,
                                    null=True, blank=True)

    def _get_comparator_display(self, c):
        for v, s in TEST_COMPARATORS:
            if v == c:
                return s
        return None

    def get_test(self):
        c1 = comparators[self.comparator_1]
        v1 = self.value_1
        if self.value_2 == None or self.comparator_2 == None:
            def testfn(x):
                return c1(x, v1)
        else:
            c2 = comparators[self.comparator_2]
            v2 = self.value_2
            def testfn(x):
                return c1(x, v1) and c2(x, v2)
        return testfn

    def __unicode__(self):
        if not self.value_2:
            return self._get_comparator_display(self.comparator_1) + u' ' + str(self.value_1)
        else:
            return self._get_comparator_display(self.comparator_1) + u' ' + \
                str(self.value_1) + \
                u' and ' + self._get_comparator_display(self.comparator_2) + \
                u' ' + str(self.value_2)

class Log(models.Model):
    alert = models.ForeignKey(Alert)
    message = models.TextField()
    when = models.DateTimeField()

def ping_backend(sender, instance, **kwargs):
    # HACK ALERT -- we don't want to do this if we're in the
    # backend... but we might be used inside of twisted.
    if no_twisted or not reactor.running:
            # have to commit before we ping otherwise they might read stale data
        try:
            transaction.commit()
        except transaction.TransactionManagementError:
            pass

        try:
            # use this guy's http -- new dep :(
            http = httplib2.Http()
            http.request(settings.ADMIN_BACKEND + 'data/alerts/update?state=%i' % 
                         instance.id, 'PUT')
        except Exception, e: 
            print "Error pong-ing backend:", e

signals.post_save.connect(ping_backend, sender=Alert)
signals.post_delete.connect(ping_backend, sender=Alert)

if __name__ == '__main__':
    print 'foo'
    
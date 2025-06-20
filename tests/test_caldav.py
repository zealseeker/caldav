#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Tests here communicate with third party servers and/or
internal ad-hoc instances of Xandikos and Radicale, dependent on the
configuration in conf_private.py.
Tests that do not require communication with a working caldav server
belong in test_caldav_unit.py
"""
import codecs
import json
import logging
import os
import random
import sys
import tempfile
import threading
import time
import uuid
from collections import namedtuple
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from urllib.parse import urlparse

import icalendar
import pytest
import vobject

from .conf import caldav_servers
from .conf import client
from .conf import proxy
from .conf import proxy_noport
from .conf import radicale_host
from .conf import radicale_port
from .conf import rfc6638_users
from .conf import test_radicale
from .conf import test_xandikos
from .conf import xandikos_host
from .conf import xandikos_port
from .proxy import NonThreadingHTTPServer
from .proxy import ProxyHandler
from caldav import compatibility_hints
from caldav.davclient import DAVClient
from caldav.davclient import DAVResponse
from caldav.davclient import get_davclient
from caldav.elements import cdav
from caldav.elements import dav
from caldav.elements import ical
from caldav.lib import error
from caldav.lib import url
from caldav.lib.python_utilities import to_local
from caldav.lib.python_utilities import to_str
from caldav.lib.url import URL
from caldav.objects import Calendar
from caldav.objects import CalendarSet
from caldav.objects import DAVObject
from caldav.objects import Event
from caldav.objects import FreeBusy
from caldav.objects import Principal
from caldav.objects import Todo

log = logging.getLogger("caldav")


ev1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VEVENT
UID:20010712T182145Z-123401@example.com
DTSTAMP:20060712T182145Z
DTSTART:20060714T170000Z
DTEND:20060715T040000Z
SUMMARY:Bastille Day Party
END:VEVENT
END:VCALENDAR
"""

broken_ev1 = """BEGIN:VEVENT
UID:20010712T182145Z-123401@example.com
DTSTART:20060714T170000Z
DTEND:20060715T040000Z
SUMMARY:Bastille Day Party
END:VEVENT
"""

ev2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VEVENT
UID:20010712T182145Z-123401@example.com
DTSTAMP:20070712T182145Z
DTSTART:20070714T170000Z
DTEND:20070715T040000Z
SUMMARY:Bastille Day Party +1year
END:VEVENT
END:VCALENDAR
"""

ev3 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VEVENT
UID:20080712T182145Z-123401@example.com
DTSTAMP:20210712T182145Z
DTSTART:20210714T170000Z
DTEND:20210715T040000Z
SUMMARY:Bastille Day Jitsi Party
END:VEVENT
END:VCALENDAR
"""

## This list is for deleting the events/todo-items in case it isn't
## sufficient/possible to create/delete the whole test calendar.
uids_used = (
    "19920901T130000Z-123407@host.com",
    "19920901T130000Z-123408@host.com",
    "19970901T130000Z-123403@example.com",
    "19970901T130000Z-123404@host.com",
    "19970901T130000Z-123405@example.com",
    "19970901T130000Z-123405@host.com",
    "19970901T130000Z-123406@host.com",
    "20010712T182145Z-123401@example.com",
    "20070313T123432Z-456553@example.com",
    "20080712T182145Z-123401@example.com",
    "19970901T130000Z-123403@example.com",
    "20010712T182145Z-123401@example.com",
    "20080712T182145Z-123401@example.com",
    "takeoutthethrash",
    "ctuid1",
    "ctuid2",
    "ctuid3",
    "ctuid4",
    "ctuid5",
    "ctuid6",
    "test1",
    "test2",
    "test3",
    "test4",
    "test5",
    "test6",
    "c26921f4-0653-11ef-b756-58ce2a14e2e5",
    "e2a2e13e-34f2-11f0-ae12-1c1bb5134174",
)
## TODO: todo7 is an item without uid.  Should be taken care of somehow.


# example from http://www.rfc-editor.org/rfc/rfc5545.txt
evr = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VEVENT
UID:19970901T130000Z-123403@example.com
DTSTAMP:19970901T130000Z
DTSTART;VALUE=DATE:19971102
SUMMARY:Our Blissful Anniversary
TRANSP:TRANSPARENT
CLASS:CONFIDENTIAL
CATEGORIES:ANNIVERSARY,PERSONAL,SPECIAL OCCASION
RRULE:FREQ=YEARLY
END:VEVENT
END:VCALENDAR"""

# example created by editing a specific occurrence of a recurrent event via Thunderbird
evr2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Mozilla.org/NONSGML Mozilla Calendar V1.1//EN
BEGIN:VEVENT
UID:c26921f4-0653-11ef-b756-58ce2a14e2e5
DTSTART:20240411T123000Z
DTEND:20240412T123000Z
DTSTAMP:20240429T181103Z
LAST-MODIFIED:20240429T181103Z
RRULE:FREQ=WEEKLY;INTERVAL=2
SEQUENCE:1
SUMMARY:Test
X-MOZ-GENERATION:1
END:VEVENT
BEGIN:VEVENT
UID:c26921f4-0653-11ef-b756-58ce2a14e2e5
RECURRENCE-ID:20240425T123000Z
DTSTART:20240425T123000Z
DTEND:20240426T123000Z
CREATED:20240429T181031Z
DTSTAMP:20240429T181103Z
LAST-MODIFIED:20240429T181103Z
SEQUENCE:1
SUMMARY:Test (edited)
X-MOZ-GENERATION:1
END:VEVENT
END:VCALENDAR"""

# example from http://www.rfc-editor.org/rfc/rfc5545.txt
todo = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VTODO
UID:20070313T123432Z-456553@example.com
DTSTAMP:20070313T123432Z
DUE;VALUE=DATE:20070501
SUMMARY:Submit Quebec Income Tax Return for 2006
CLASS:CONFIDENTIAL
CATEGORIES:FAMILY,FINANCE
STATUS:NEEDS-ACTION
END:VTODO
END:VCALENDAR"""

# example from RFC2445, 4.6.2
todo2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VTODO
UID:19970901T130000Z-123404@host.com
DTSTAMP:19970901T130000Z
DTSTART:19970415T133000Z
DUE:19970416T045959Z
SUMMARY:1996 Income Tax Preparation
CLASS:CONFIDENTIAL
CATEGORIES:FAMILY,FINANCE
PRIORITY:2
STATUS:NEEDS-ACTION
END:VTODO
END:VCALENDAR"""

todo3 = """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VTODO
UID:19970901T130000Z-123405@host.com
DTSTAMP:19970901T130000Z
DTSTART:19970415T133000Z
DUE:19970516T045959Z
SUMMARY:1996 Income Tax Preparation
CLASS:CONFIDENTIAL
CATEGORIES:FAMILY,FINANCE
PRIORITY:1
END:VTODO
END:VCALENDAR"""

todo4 = """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VTODO
UID:19970901T130000Z-123406@host.com
DTSTAMP:19970901T130000Z
SUMMARY:1996 Income Tax Preparation
CLASS:CONFIDENTIAL
CATEGORIES:FAMILY,FINANCE
PRIORITY:1
STATUS:NEEDS-ACTION
END:VTODO
END:VCALENDAR"""

todo5 = """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VTODO
UID:19920901T130000Z-123407@host.com
DTSTAMP:19920901T130000Z
DTSTART:19920415T133000Z
DUE:19920516T045959Z
SUMMARY:1992 Income Tax Preparation
CLASS:CONFIDENTIAL
CATEGORIES:FAMILY,FINANCE
PRIORITY:1
END:VTODO
END:VCALENDAR"""

todo6 = """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VTODO
UID:19920901T130000Z-123408@host.com
DTSTAMP:19920901T130000Z
DTSTART:19920415T133000Z
DUE:19920516T045959Z
SUMMARY:Yearly Income Tax Preparation
RRULE:FREQ=YEARLY
CLASS:CONFIDENTIAL
CATEGORIES:FAMILY,FINANCE
PRIORITY:1
END:VTODO
END:VCALENDAR"""

## a todo without uid.  Should it be possible to store it at all?
todo7 = """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VTODO
DTSTAMP:19980101T130000Z
DTSTART:19980415T133000Z
DUE:19980516T045959Z
STATUS:NEEDS-ACTION
SUMMARY:Get stuck with Netfix and forget about the tax income declaration
CLASS:CONFIDENTIAL
CATEGORIES:FAMILY
PRIORITY:1
END:VTODO
END:VCALENDAR"""

todo8 = """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VTODO
UID:takeoutthethrash
DTSTAMP:20221013T151313Z
DTSTART:20221017T065500Z
STATUS:NEEDS-ACTION
DURATION:PT10M
SUMMARY:Take out the thrash before the collectors come.
RRULE:FREQ=WEEKLY;BYDAY=MO;BYHOUR=6;BYMINUTE=55;COUNT=3
CATEGORIES:CHORE
PRIORITY:3
END:VTODO
END:VCALENDAR"""

# example from http://www.kanzaki.com/docs/ical/vjournal.html
journal = """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VJOURNAL
UID:19970901T130000Z-123405@example.com
DTSTAMP:19970901T130000Z
DTSTART;VALUE=DATE:19970317
SUMMARY:Staff meeting minutes
DESCRIPTION:1. Staff meeting: Participants include Joe\\, Lisa
  and Bob. Aurora project plans were reviewed. There is currently
  no budget reserves for this project. Lisa will escalate to
  management. Next meeting on Tuesday.\n
END:VJOURNAL
END:VCALENDAR
"""

## From RFC4438 examples, with some modifications
sched_template = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//CalDAV Client//EN
BEGIN:VEVENT
UID:%s
SEQUENCE:0
DTSTAMP:20210206T%sZ
DTSTART:203206%02iT%sZ
DURATION:PT1H
TRANSP:OPAQUE
SUMMARY:Lunch or something
END:VEVENT
END:VCALENDAR
"""

attendee1 = """
BEGIN:VCALENDAR
PRODID:-//Example Corp.//CalDAV Client//EN
VERSION:2.0
BEGIN:VEVENT
STATUS:CANCELLED
UID:test-attendee1
X-MICROSOFT-DISALLOW-COUNTER:true
DTSTART;TZID=Europe/Moscow:20240607T100000
DTEND;TZID=Europe/Moscow:20240607T103000
LAST-MODIFIED:20240610T063933Z
DTSTAMP:20240618T063824Z
CREATED:00010101T000000Z
SUMMARY:test
SEQUENCE:0
TRANSP:OPAQUE
X-MOZ-LASTACK:20240610T063933Z
ORGANIZER;CN=:mailto:t-caldav-test-att1@tobixen.no
ATTENDEE;PARTSTAT=ACCEPTED;RSVP=true;ROLE=REQ-PARTICIPANT:mailto:t-test-attendee1@tobixen.no
ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=DECLINED:mailto:testemail2024@list.ru
END:VEVENT
BEGIN:VTIMEZONE
TZID:Europe/Moscow
TZURL:http://tzurl.org/zoneinfo-outlook/Europe/Moscow
X-LIC-LOCATION:Europe/Moscow
BEGIN:STANDARD
TZNAME:MSK
TZOFFSETFROM:+0300
TZOFFSETTO:+0300
DTSTART:19700101T000000
END:STANDARD
END:VTIMEZONE
END:VCALENDAR
"""

attendee2 = """
BEGIN:VCALENDAR
PRODID:-//MailRu//MailRu Calendar API -//EN
VERSION:2.0
BEGIN:VEVENT
STATUS:CANCELLED
UID:EB424921-C4D3-46A6-B827-9A92A90D6788
X-MICROSOFT-DISALLOW-COUNTER:true
DTSTART;TZID=Europe/Moscow:20240607T100000
DTEND;TZID=Europe/Moscow:20240607T103000
LAST-MODIFIED:20240610T063933Z
DTSTAMP:20240618T064033Z
CREATED:00010101T000000Z
SUMMARY:test
SEQUENCE:0
TRANSP:OPAQUE
X-MOZ-LASTACK:20240610T063933Z
ORGANIZER;CN=:mailto:knazarov@i-core.ru
ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=true:mailto:knazarov@i
 -core.ru
ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=DECLINED:mailto:testemail2024@list.r
 u
END:VEVENT
BEGIN:VTIMEZONE
TZID:Europe/Moscow
TZURL:http://tzurl.org/zoneinfo-outlook/Europe/Moscow
X-LIC-LOCATION:Europe/Moscow
BEGIN:STANDARD
TZNAME:MSK
TZOFFSETFROM:+0300
TZOFFSETTO:+0300
DTSTART:19700101T000000
END:STANDARD
END:VTIMEZONE
END:VCALENDAR
"""


sched = sched_template % (
    str(uuid.uuid4()),
    "%2i%2i%2i" % (random.randint(0, 23), random.randint(0, 59), random.randint(0, 59)),
    random.randint(1, 28),
    "%2i%2i%2i" % (random.randint(0, 23), random.randint(0, 59), random.randint(0, 59)),
)


@pytest.mark.skipif(
    not caldav_servers,
    reason="Requirement: at least one working server in conf.py. The tail object of the server list will be chosen, that is typically the LocalRadicale or LocalXandikos server.",
)
class TestGetDAVClient:
    """
    Tests for get_davclient and auto_calendars.

    """

    def testTestConfig(self):
        with get_davclient(
            testconfig=True, environment=False, name=-1, config_file=False
        ) as conn:
            assert conn.principal()

    def testEnvironment(self):
        os.environ["PYTHON_CALDAV_USE_TEST_SERVER"] = "1"
        with get_davclient(environment=True, config_file=False, name="-1") as conn:
            assert conn.principal()
            del os.environ["PYTHON_CALDAV_USE_TEST_SERVER"]
            for key in ("url", "username", "password", "proxy"):
                if key in caldav_servers[-1]:
                    os.environ[f"CALDAV_{key.upper()}"] = caldav_servers[-1][key]
            with get_davclient(
                testconfig=False, environment=True, config_file=False
            ) as conn2:
                assert conn2.principal()

    def testConfigfile(self):
        ## start up a server
        with get_davclient(
            testconfig=True, environment=False, name=-1, config_file=False
        ) as conn:
            config = {}
            for key in ("url", "username", "password", "proxy"):
                if key in caldav_servers[-1]:
                    config[f"caldav_{key}"] = caldav_servers[-1][key]

            with tempfile.NamedTemporaryFile(
                delete=True, encoding="utf-8", mode="w"
            ) as tmp:
                json.dump({"default": config}, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
                with get_davclient(
                    config_file=tmp.name, testconfig=False, environment=False
                ) as conn2:
                    assert conn2.principal()


@pytest.mark.skipif(
    not rfc6638_users, reason="need rfc6638_users to be set in order to run this test"
)
@pytest.mark.skipif(
    len(rfc6638_users) < 3,
    reason="need at least three users in rfc6638_users to be set in order to run this test",
)
class TestScheduling:
    """Testing support of RFC6638.
    TODO: work in progress.  Stalled a bit due to lack of proper testing accounts.  I haven't managed to get this test to pass at any systems yet, but I believe the problem is not on the library side.
    * icloud: cannot really test much with only one test account
      available.  I did some testing forth and back with emails sent
      to an account on another service through the
      scheduling_examples.py, and it seems like I was able both to
      accept an invite from an external account (and the external
      account got notified about it) and to receive notification that
      the external party having accepted the calendar invite.
      FreeBusy doesn't work.  I don't have capacity following up more
      right now.
    * DAViCal: I have only an old version to test with at the moment,
      should look into that.  I did manage to send and receive a
      calendar invite, but apparently I did not manage to accept the
      calendar invite.  It should be looked more into.  FreeBusy
      doesn't work in the old version, probably it works in a newer
      version.
    * SOGo: Sending a calendar invite, but receiving nothing in the
      CalDAV inbox.  FreeBusy works somehow, but returns pure
      iCalendar data and not XML, I believe that's not according to
      RFC6638.
    """

    def _getCalendar(self, i):
        calendar_id = "schedulingnosetestcalendar%i" % i
        calendar_name = "caldav scheduling test %i" % i
        try:
            self.principals[i].calendar(name=calendar_name).delete()
        except error.NotFoundError:
            pass
        return self.principals[i].make_calendar(name=calendar_name, cal_id=calendar_id)

    def setup_method(self):
        self.clients = []
        self.principals = []
        for foo in rfc6638_users:
            c = client(**foo)
            if not c.check_scheduling_support():
                continue  ## ignoring user because server does not support scheduling.
            self.clients.append(c)
            self.principals.append(c.principal())

    def teardown_method(self):
        for i in range(0, len(self.principals)):
            calendar_name = "caldav scheduling test %i" % i
            try:
                self.principals[i].calendar(name=calendar_name).delete()
            except error.NotFoundError:
                pass
        for c in self.clients:
            c.__exit__()

    ## TODO
    # def testFreeBusy(self):
    # pass

    def testInviteAndRespond(self):
        ## Look through inboxes of principals[0] and principals[1] so we can sort
        ## out existing stuff from new stuff
        if len(self.principals) < 2:
            pytest.skip("need 2 principals to do the invite and respond test")
        inbox_items = set(
            [x.url for x in self.principals[0].schedule_inbox().get_items()]
        )
        inbox_items.update(
            set([x.url for x in self.principals[1].schedule_inbox().get_items()])
        )

        ## self.principal[0] is the organizer, and invites self.principal[1]
        organizers_calendar = self._getCalendar(0)
        attendee_calendar = self._getCalendar(1)
        organizers_calendar.save_with_invites(
            sched, [self.principals[0], self.principals[1].get_vcal_address()]
        )
        assert len(organizers_calendar.events()) == 1

        ## no new inbox items expected for principals[0]
        for item in self.principals[0].schedule_inbox().get_items():
            assert item.url in inbox_items

        ## principals[1] should have one new inbox item
        new_inbox_items = []
        for item in self.principals[1].schedule_inbox().get_items():
            if not item.url in inbox_items:
                new_inbox_items.append(item)
        assert len(new_inbox_items) == 1
        ## ... and the new inbox item should be an invite request
        assert new_inbox_items[0].is_invite_request()

        ## Approving the invite
        new_inbox_items[0].accept_invite(calendar=attendee_calendar)
        ## (now, this item should probably appear on a calendar somewhere ...
        ## TODO: make asserts on that)
        ## TODO: what happens if we delete that invite request now?

        ## principals[0] should now have a notification in the inbox that the
        ## calendar invite was accepted
        new_inbox_items = []
        for item in self.principals[0].schedule_inbox().get_items():
            if not item.url in inbox_items:
                new_inbox_items.append(item)
        assert len(new_inbox_items) == 1
        assert new_inbox_items[0].is_invite_reply()
        new_inbox_items[0].delete()

    ## TODO.  Invite two principals, let both of them load the
    ## invitation, and then let them respond in order.  Lacks both
    ## tests and the implementation also apparently doesn't work as
    ## for now (perhaps I misunderstood the RFC).
    # def testAcceptedInviteRaceCondition(self):
    # pass

    ## TODO: more testing ... what happens if deleting things from the
    ## inbox/outbox?


def _delay_decorator(f, t=20):
    def foo(*a, **kwa):
        time.sleep(t)
        return f(*a, **kwa)

    return foo


class RepeatedFunctionalTestsBaseClass:
    """This is a class with functional tests (tests that goes through
    basic functionality and actively communicates with third parties)
    that we want to repeat for all configured caldav_servers.
    (what a truly ugly name for this class - any better ideas?)
    NOTE: this tests relies heavily on the assumption that we can create
    calendars on the remote caldav server, but the RFC says ...
       Support for MKCALENDAR on the server is only RECOMMENDED and not
       REQUIRED because some calendar stores only support one calendar per
       user (or principal), and those are typically pre-created for each
       account.
    We've had some problems with iCloud and Radicale earlier.  Google
    still does not support mkcalendar.
    """

    _default_calendar = None

    def check_compatibility_flag(self, flag):
        ## yield an assertion error if checking for the wrong thig
        assert flag in compatibility_hints.incompatibility_description
        return flag in self.incompatibilities

    def skip_on_compatibility_flag(self, flag):
        if self.check_compatibility_flag(flag):
            msg = compatibility_hints.incompatibility_description[flag]
            pytest.skip("Test skipped due to server incompatibility issue: " + msg)

    def setup_method(self):
        logging.debug("############## test setup")
        self.incompatibilities = set()
        self.cleanup_regime = self.server_params.get("cleanup", "light")
        self.calendars_used = []

        for flag in self.server_params.get("incompatibilities", []):
            assert flag in compatibility_hints.incompatibility_description
            self.incompatibilities.add(flag)

        if self.check_compatibility_flag("unique_calendar_ids"):
            self.testcal_id = "testcalendar-" + str(uuid.uuid4())
            self.testcal_id2 = "testcalendar-" + str(uuid.uuid4())
        else:
            self.testcal_id = "pythoncaldav-test"
            self.testcal_id2 = "pythoncaldav-test2"

        self.caldav = client(**self.server_params)
        self.caldav.__enter__()

        if self.check_compatibility_flag("rate_limited"):
            self.caldav.request = _delay_decorator(self.caldav.request)
        if self.check_compatibility_flag("search_delay"):
            Calendar._search = Calendar.search
            Calendar.search = _delay_decorator(Calendar.search)

        if False and self.check_compatibility_flag("no-current-user-principal"):
            self.principal = Principal(
                client=self.caldav, url=self.server_params["principal_url"]
            )
        else:
            self.principal = self.caldav.principal()

        # if self.check_compatibility_flag('delete_calendar_on_startup'):
        # for x in self._fixCalendar().search():
        # x.delete()

        self._cleanup("pre")

        logging.debug("##############################")
        logging.debug("############## test setup done")
        logging.debug("##############################")

    def teardown_method(self):
        if self.check_compatibility_flag("search_delay"):
            Calendar.search = Calendar._search
        logging.debug("############################")
        logging.debug("############## test teardown_method")
        logging.debug("############################")
        self._cleanup("post")
        logging.debug("############## test teardown_method almost done")
        self.caldav.teardown(self.caldav)

    def _cleanup(self, mode=None):
        if self.cleanup_regime in ("pre", "post") and self.cleanup_regime != mode:
            return
        if self.check_compatibility_flag("read_only"):
            return  ## no cleanup needed
        if (
            self.check_compatibility_flag("no_mkcalendar")
            or self.cleanup_regime == "thorough"
        ):
            for uid in uids_used:
                try:
                    obj = self._fixCalendar().object_by_uid(uid)
                    obj.delete()
                except error.NotFoundError:
                    pass
                except:
                    logging.error(
                        "Something went kaboom while deleting event", exc_info=True
                    )
            return
        for cal in self.calendars_used:
            cal.delete()
        if self.check_compatibility_flag("unique_calendar_ids") and mode == "pre":
            a = self._teardownCalendar(name="Yep")
        if mode == "post":
            for calid in (self.testcal_id, self.testcal_id2):
                self._teardownCalendar(cal_id=calid)
        if self.cleanup_regime == "thorough":
            for name in ("Yep", "Yapp", "Yølp", self.testcal_id, self.testcal_id2):
                self._teardownCalendar(name=name)
                self._teardownCalendar(cal_id=name)

    def _teardownCalendar(self, name=None, cal_id=None):
        try:
            cal = self.principal.calendar(name=name, cal_id=cal_id)
            if self.check_compatibility_flag(
                "sticky_events"
            ) or self.check_compatibility_flag("no_delete_calendar"):
                for goo in cal.objects():
                    try:
                        goo.delete()
                    except:
                        pass
            cal.delete()
        except:
            pass

    def _fixCalendar(self, **kwargs):
        """
        Should ideally return a new calendar, if that's not possible it
        should see if there exists a test calendar, if that's not
        possible, give up and return the primary calendar.
        """
        if self.check_compatibility_flag(
            "no_mkcalendar"
        ) or self.check_compatibility_flag("read_only"):
            if not self._default_calendar:
                calendars = self.principal.calendars()
                for c in calendars:
                    if (
                        "pythoncaldav-test"
                        in c.get_properties(
                            [
                                dav.DisplayName(),
                            ]
                        ).values()
                    ):
                        self._default_calendar = c
                        return c
                self._default_calendar = calendars[0]
            return self._default_calendar
        else:
            if not "name" in kwargs:
                if not self.check_compatibility_flag(
                    "unique_calendar_ids"
                ) and self.cleanup_regime in ("light", "pre"):
                    self._teardownCalendar(cal_id=self.testcal_id)
                if self.check_compatibility_flag("no_displayname"):
                    kwargs["name"] = None
                else:
                    kwargs["name"] = "Yep"
            if not "cal_id" in kwargs:
                kwargs["cal_id"] = self.testcal_id
            try:
                ret = self.principal.make_calendar(**kwargs)
            except error.MkcalendarError:
                ## "calendar already exists" can be ignored (at least
                ## if no_delete_calendar flag is set)
                ret = self.principal.calendar(cal_id=kwargs["cal_id"])
            if self.check_compatibility_flag("search_always_needs_comptype"):
                ret.objects = lambda load_objects: ret.events()
            if self.cleanup_regime == "post":
                self.calendars_used.append(ret)

            return ret

    def testSupport(self):
        """
        Test the check_*_support methods
        """
        self.skip_on_compatibility_flag("dav_not_supported")
        assert self.caldav.check_dav_support()
        assert self.caldav.check_cdav_support()
        if self.check_compatibility_flag("no_scheduling"):
            assert not self.caldav.check_scheduling_support()
        else:
            assert self.caldav.check_scheduling_support()

    def testSchedulingInfo(self):
        self.skip_on_compatibility_flag("no_scheduling")
        self.skip_on_compatibility_flag("no_scheduling_calendar_user_address_set")
        calendar_user_address_set = self.principal.calendar_user_address_set()
        me_a_participant = self.principal.get_vcal_address()

    def testSchedulingMailboxes(self):
        self.skip_on_compatibility_flag("no_scheduling")
        self.skip_on_compatibility_flag("no_scheduling_mailbox")
        inbox = self.principal.schedule_inbox()
        outbox = self.principal.schedule_outbox()

    def testPropfind(self):
        """
        Test of the propfind methods. (This is sort of redundant, since
        this is implicitly run by the setup)
        """
        # ResourceType MUST be defined, and SHOULD be returned on a propfind
        # for "allprop" if I have the permission to see it.
        # So, no ResourceType returned seems like a bug in bedework
        self.skip_on_compatibility_flag("propfind_allprop_failure")

        # first a raw xml propfind to the root URL
        foo = self.caldav.propfind(
            self.principal.url,
            props='<?xml version="1.0" encoding="UTF-8"?>'
            '<D:propfind xmlns:D="DAV:">'
            "  <D:allprop/>"
            "</D:propfind>",
        )
        assert "resourcetype" in to_local(foo.raw)

        # next, the internal _query_properties, returning an xml tree ...
        foo2 = self.principal._query_properties(
            [
                dav.Status(),
            ]
        )
        assert "resourcetype" in to_local(foo.raw)
        # TODO: more advanced asserts

    def testGetCalendarHomeSet(self):
        chs = self.principal.get_properties([cdav.CalendarHomeSet()])
        assert "{urn:ietf:params:xml:ns:caldav}calendar-home-set" in chs

    def testGetDefaultCalendar(self):
        self.skip_on_compatibility_flag("no_default_calendar")
        assert len(self.principal.calendars()) != 0

    def testSearchShouldYieldData(self):
        """
        ref https://github.com/python-caldav/caldav/issues/201
        """
        c = self._fixCalendar()

        if not self.check_compatibility_flag("read_only"):
            ## populate the calendar with an event or two or three
            c.save_event(ev1)
            c.save_event(ev2)
            c.save_event(ev3)
        objects = c.search(event=True)
        ## This will break if served a read-only calendar without any events
        assert objects
        ## This was observed to be broken for @dreimer1986
        assert objects[0].data

    def testGetCalendar(self):
        # Create calendar
        c = self._fixCalendar()
        assert c.url is not None
        assert len(self.principal.calendars()) != 0

        str_ = str(c)
        repr_ = repr(c)

        ## Not sure if those asserts make much sense, the main point here is to exercise
        ## the __str__ and __repr__ methods on the Calendar object.
        if not self.check_compatibility_flag("no_displayname"):
            name = c.get_property(dav.DisplayName(), use_cached=True)
            if not name:
                name = c.url
            assert str(name) == str_
        assert "Calendar" in repr(c)
        assert str(c.url) in repr(c)

    def testProxy(self):
        if self.caldav.url.scheme == "https":
            pytest.skip(
                "Skipping %s.testProxy as the TinyHTTPProxy "
                "implementation doesn't support https"
            )
        self.skip_on_compatibility_flag("no_default_calendar")

        server_address = ("127.0.0.1", 8080)
        try:
            proxy_httpd = NonThreadingHTTPServer(
                server_address, ProxyHandler, logging.getLogger("TinyHTTPProxy")
            )
        except:
            pytest.skip("Unable to set up proxy server")

        threadobj = threading.Thread(target=proxy_httpd.serve_forever)
        conn_params = self.server_params.copy()
        for special in ("setup", "teardown", "name"):
            if special in conn_params:
                conn_params.pop(special)
        try:
            threadobj.start()
            assert threadobj.is_alive()
            conn_params["proxy"] = proxy
            c = client(**conn_params)
            p = c.principal()
            assert len(p.calendars()) != 0
        finally:
            proxy_httpd.shutdown()
            # this should not be necessary, but I've observed some failures
            if threadobj.is_alive():
                time.sleep(0.15)
            assert not threadobj.is_alive()

        threadobj = threading.Thread(target=proxy_httpd.serve_forever)
        try:
            threadobj.start()
            assert threadobj.is_alive()
            conn_params["proxy"] = proxy_noport
            c = client(**conn_params)
            p = c.principal()
            assert len(p.calendars()) != 0
            assert threadobj.is_alive()
        finally:
            proxy_httpd.shutdown()
            # this should not be necessary
            if threadobj.is_alive():
                time.sleep(0.05)
            assert not threadobj.is_alive()

    def _notFound(self):
        if self.check_compatibility_flag("non_existing_raises_other"):
            return error.DAVError
        else:
            return error.NotFoundError

    def testPrincipal(self):
        collections = self.principal.calendars()
        if "principal_url" in self.server_params:
            assert self.principal.url == self.server_params["principal_url"]
        for c in collections:
            assert c.__class__.__name__ == "Calendar"

    def testCreateDeleteCalendar(self):
        self.skip_on_compatibility_flag("no_mkcalendar")
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_delete_calendar")
        if not self.check_compatibility_flag(
            "unique_calendar_ids"
        ) and self.cleanup_regime in ("light", "pre"):
            self._teardownCalendar(cal_id=self.testcal_id)
        c = self.principal.make_calendar(name="Yep", cal_id=self.testcal_id)

        assert c.url is not None
        events = c.events()
        assert len(events) == 0
        events = self.principal.calendar(name="Yep", cal_id=self.testcal_id).events()
        assert len(events) == 0
        c.delete()

        # this breaks with zimbra and radicale
        if not self.check_compatibility_flag("non_existing_calendar_found"):
            with pytest.raises(self._notFound()):
                self.principal.calendar(name="Yep", cal_id=self.testcal_id).events()

    def testChangeAttendeeStatusWithEmailGiven(self):
        self.skip_on_compatibility_flag("read_only")
        c = self._fixCalendar()

        event = c.save_event(
            uid="test1",
            dtstart=datetime(2015, 10, 10, 8, 7, 6),
            dtend=datetime(2015, 10, 10, 9, 7, 6),
            ical_fragment="ATTENDEE;ROLE=OPT-PARTICIPANT;PARTSTAT=TENTATIVE:MAILTO:testuser@example.com",
        )
        event.change_attendee_status(
            attendee="testuser@example.com", PARTSTAT="ACCEPTED"
        )
        event.save()
        self.skip_on_compatibility_flag("object_by_uid_is_broken")
        event = c.event_by_uid("test1")
        ## TODO: work in progress ... see https://github.com/python-caldav/caldav/issues/399

    def testMultiGet(self):
        self.skip_on_compatibility_flag("read_only")
        c = self._fixCalendar()

        event1 = c.save_event(
            uid="test1",
            dtstart=datetime(2015, 10, 10, 8, 7, 6),
            dtend=datetime(2015, 10, 10, 9, 7, 6),
            summary="test1",
        )

        event2 = c.save_event(
            uid="test2",
            dtstart=datetime(2015, 10, 10, 8, 7, 6),
            dtend=datetime(2015, 10, 10, 9, 7, 6),
            summary="test2",
        )

        results = c.calendar_multiget([event1.url, event2.url])
        assert len(results) == 2
        assert set([x.icalendar_component["uid"] for x in results]) == {
            "test1",
            "test2",
        }

    def testCreateEvent(self):
        self.skip_on_compatibility_flag("read_only")
        c = self._fixCalendar()

        existing_events = c.events()
        existing_urls = {x.url for x in existing_events}
        cleanse = lambda events: [x for x in events if x.url not in existing_urls]

        if not self.check_compatibility_flag("no_mkcalendar"):
            ## we're supposed to be working towards a brand new calendar
            assert len(existing_events) == 0

        # add event
        c.save_event(broken_ev1)

        # c.events() should give a full list of events
        events = cleanse(c.events())
        assert len(events) == 1

        # We should be able to access the calender through the URL
        c2 = self.caldav.calendar(url=c.url)
        events2 = cleanse(c2.events())
        assert len(events2) == 1
        assert events2[0].url == events[0].url

        if not self.check_compatibility_flag(
            "no_mkcalendar"
        ) and not self.check_compatibility_flag("no_displayname"):
            # We should be able to access the calender through the name
            c2 = self.principal.calendar(name="Yep")
            ## may break if we have multiple calendars with the same name
            if not self.check_compatibility_flag("no_delete_calendar"):
                assert c2.url == c.url
                events2 = cleanse(c2.events())
                assert len(events2) == 1
                assert events2[0].url == events[0].url

        # add another event, it should be doable without having premade ICS
        ev2 = c.save_event(
            dtstart=datetime(2015, 10, 10, 8, 7, 6),
            summary="This is a test event",
            dtend=datetime(2016, 10, 10, 9, 8, 7),
            uid="ctuid1",
        )
        events = c.events()
        assert len(events) == len(existing_events) + 2
        ev2.delete()

    def testAlarm(self):
        ## Ref https://github.com/python-caldav/caldav/issues/132
        c = self._fixCalendar()
        ev = c.save_event(
            dtstart=datetime(2015, 10, 10, 8, 0, 0),
            summary="This is a test event",
            uid="test1",
            dtend=datetime(2016, 10, 10, 9, 0, 0),
            alarm_trigger=timedelta(minutes=-15),
            alarm_action="AUDIO",
        )

        self.skip_on_compatibility_flag("no_alarmsearch")

        ## So we have an alarm that goes off 07:45 for an event starting 08:00

        ## Search for alarms after 8 should find nothing
        ## (search for an alarm 07:55 - 08:05 should most likely find nothing).
        assert (
            len(
                c.search(
                    event=True,
                    alarm_start=datetime(2015, 10, 10, 8, 1),
                    alarm_end=datetime(2015, 10, 10, 8, 7),
                )
            )
            == 0
        )

        ## Search for alarms from 07:40 to 07:55 should definitively find the alarm.
        assert (
            len(
                c.search(
                    event=True,
                    alarm_start=datetime(2015, 10, 10, 7, 40),
                    alarm_end=datetime(2015, 10, 10, 7, 55),
                )
            )
            == 1
        )

    def testCalendarByFullURL(self):
        """
        ref private email, passing a full URL as cal_id works in 0.5.0 but
        is broken in 0.8.0
        """
        mycal = self._fixCalendar()

        samecal = self.caldav.principal().calendar(cal_id=str(mycal.url))
        assert mycal.url.canonical() == samecal.url.canonical()
        ## passing cal_id as a URL object should also work.
        samecal = self.caldav.principal().calendar(cal_id=mycal.url)
        assert mycal.url.canonical() == samecal.url.canonical()

    def testObjectBySyncToken(self):
        """
        Support for sync-collection reports, ref https://github.com/python-caldav/caldav/issues/87.
        This test is using explicit calls to objects_by_sync_token
        """
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_sync_token")

        ## Boiler plate ... make a calendar and add some content
        c = self._fixCalendar()
        objcnt = 0
        ## in case we need to reuse an existing calendar ...
        if not self.check_compatibility_flag("no_todo"):
            objcnt += len(c.todos())
        objcnt += len(c.events())
        obj = c.save_event(ev1)
        objcnt += 1
        if not self.check_compatibility_flag("no_recurring"):
            c.save_event(evr)
            objcnt += 1
        if not self.check_compatibility_flag("no_todo"):
            c.save_todo(todo)
            c.save_todo(todo2)
            c.save_todo(todo3)
            objcnt += 3

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## objects should return all objcnt object.
        my_objects = c.objects()
        assert my_objects.sync_token != ""
        assert len(list(my_objects)) == objcnt

        ## They should not be loaded.
        for some_obj in my_objects:
            assert some_obj.data is None

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## running sync_token again with the new token should return 0 hits
        my_changed_objects = c.objects_by_sync_token(sync_token=my_objects.sync_token)
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(my_changed_objects)) == 0

        ## I was unable to run the rest of the tests towards Google using their legacy caldav API
        self.skip_on_compatibility_flag("no_overwrite")

        ## MODIFYING an object
        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)
        obj.icalendar_instance.subcomponents[0]["SUMMARY"] = "foobar"
        obj.save()

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## The modified object should be returned by the server
        my_changed_objects = c.objects_by_sync_token(
            sync_token=my_changed_objects.sync_token, load_objects=True
        )
        if self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(my_changed_objects)) >= 1
        else:
            assert len(list(my_changed_objects)) == 1

        ## this time it should be loaded
        assert list(my_changed_objects)[0].data is not None

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## Re-running objects_by_sync_token, and no objects should be returned
        my_changed_objects = c.objects_by_sync_token(
            sync_token=my_changed_objects.sync_token
        )

        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(my_changed_objects)) == 0

        ## ADDING yet another object ... and it should also be reported
        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)
        obj3 = c.save_event(ev3)
        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)
        my_changed_objects = c.objects_by_sync_token(
            sync_token=my_changed_objects.sync_token
        )
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(my_changed_objects)) == 1

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## Re-running objects_by_sync_token, and no objects should be returned
        my_changed_objects = c.objects_by_sync_token(
            sync_token=my_changed_objects.sync_token
        )
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(my_changed_objects)) == 0

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## DELETING the object ... and it should be reported
        obj.delete()
        self.skip_on_compatibility_flag("sync_breaks_on_delete")
        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)
        my_changed_objects = c.objects_by_sync_token(
            sync_token=my_changed_objects.sync_token, load_objects=True
        )
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(my_changed_objects)) == 1
        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)
        ## even if we have asked for the object to be loaded, data should be None as it's a deleted object
        assert list(my_changed_objects)[0].data is None

        ## Re-running objects_by_sync_token, and no objects should be returned
        my_changed_objects = c.objects_by_sync_token(
            sync_token=my_changed_objects.sync_token
        )
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(my_changed_objects)) == 0

    def testSync(self):
        """
        Support for sync-collection reports, ref https://github.com/python-caldav/caldav/issues/87.
        Same test pattern as testObjectBySyncToken, but exercises the .sync() method
        """
        self.skip_on_compatibility_flag("no_sync_token")
        self.skip_on_compatibility_flag("read_only")

        ## Boiler plate ... make a calendar and add some content
        c = self._fixCalendar()

        objcnt = 0
        ## in case we need to reuse an existing calendar ...
        if not self.check_compatibility_flag("no_todo"):
            objcnt += len(c.todos())
        objcnt += len(c.events())
        obj = c.save_event(ev1)
        objcnt += 1
        if not self.check_compatibility_flag("no_recurring"):
            c.save_event(evr)
            objcnt += 1
        if not self.check_compatibility_flag("no_todo"):
            c.save_todo(todo)
            c.save_todo(todo2)
            c.save_todo(todo3)
            objcnt += 3

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## objects should return all objcnt object.
        my_objects = c.objects(load_objects=True)
        assert my_objects.sync_token != ""
        assert len(list(my_objects)) == objcnt

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## sync() should do nothing
        updated, deleted = my_objects.sync()
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(updated)) == 0
            assert len(list(deleted)) == 0

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## I was unable to run the rest of the tests towards Google using their legacy caldav API
        self.skip_on_compatibility_flag("no_overwrite")

        ## MODIFYING an object
        obj.icalendar_instance.subcomponents[0]["SUMMARY"] = "foobar"
        obj.save()

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        updated, deleted = my_objects.sync()
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(updated)) == 1
            assert len(list(deleted)) == 0
        assert "foobar" in my_objects.objects_by_url()[obj.url].data

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## ADDING yet another object ... and it should also be reported
        obj3 = c.save_event(ev3)

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        updated, deleted = my_objects.sync()
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(updated)) == 1
            assert len(list(deleted)) == 0
        assert obj3.url in my_objects.objects_by_url()

        self.skip_on_compatibility_flag("sync_breaks_on_delete")

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## DELETING the object ... and it should be reported
        obj.delete()
        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)
        updated, deleted = my_objects.sync()
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(updated)) == 0
            assert len(list(deleted)) == 1
        assert not obj.url in my_objects.objects_by_url()

        if self.check_compatibility_flag("time_based_sync_tokens"):
            time.sleep(1)

        ## sync() should do nothing
        updated, deleted = my_objects.sync()
        if not self.check_compatibility_flag("fragile_sync_tokens"):
            assert len(list(updated)) == 0
            assert len(list(deleted)) == 0

    def testLoadEvent(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_mkcalendar")
        if not self.check_compatibility_flag(
            "unique_calendar_ids"
        ) and self.cleanup_regime in ("light", "pre"):
            self._teardownCalendar(cal_id=self.testcal_id)
            self._teardownCalendar(cal_id=self.testcal_id2)
        c1 = self._fixCalendar(name="Yep", cal_id=self.testcal_id)
        c2 = self._fixCalendar(name="Yapp", cal_id=self.testcal_id2)

        e1_ = c1.save_event(ev1)
        e1_.load()
        e1 = c1.events()[0]
        assert e1.url == e1_.url
        e1.load()
        if (
            not self.check_compatibility_flag("unique_calendar_ids")
            and self.cleanup_regime == "post"
        ):
            self._teardownCalendar(cal_id=self.testcal_id)
            self._teardownCalendar(cal_id=self.testcal_id2)

    def testCopyEvent(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_mkcalendar")
        if not self.check_compatibility_flag(
            "unique_calendar_ids"
        ) and self.cleanup_regime in ("light", "pre"):
            self._teardownCalendar(cal_id=self.testcal_id)
            self._teardownCalendar(cal_id=self.testcal_id2)

        ## Let's create two calendars, and populate one event on the first calendar
        c1 = self._fixCalendar(name="Yep", cal_id=self.testcal_id)
        c2 = self._fixCalendar(name="Yapp", cal_id=self.testcal_id2)

        assert not len(c1.events())
        assert not len(c2.events())
        e1_ = c1.save_event(ev1)
        e1 = c1.events()[0]

        if not self.check_compatibility_flag("duplicates_not_allowed"):
            ## Duplicate the event in the same calendar, with new uid
            e1_dup = e1.copy()
            e1_dup.save()
            assert len(c1.events()) == 2

        if not self.check_compatibility_flag(
            "duplicate_in_other_calendar_with_same_uid_breaks"
        ):
            e1_in_c2 = e1.copy(new_parent=c2, keep_uid=True)
            e1_in_c2.save()
            if not self.check_compatibility_flag(
                "duplicate_in_other_calendar_with_same_uid_is_lost"
            ):
                assert len(c2.events()) == 1

                ## what will happen with the event in c1 if we modify the event in c2,
                ## which shares the id with the event in c1?
                e1_in_c2.instance.vevent.summary.value = "asdf"
                e1_in_c2.save()
                e1.load()
                ## should e1.summary be 'asdf' or 'Bastille Day Party'?  I do
                ## not know, but all implementations I've tested will treat
                ## the copy in the other calendar as a distinct entity, even
                ## if the uid is the same.
                assert e1.instance.vevent.summary.value == "Bastille Day Party"
                assert c2.events()[0].instance.vevent.uid == e1.instance.vevent.uid

        ## Duplicate the event in the same calendar, with same uid -
        ## this makes no sense, there won't be any duplication
        e1_dup2 = e1.copy(keep_uid=True)
        e1_dup2.save()
        if self.check_compatibility_flag("duplicates_not_allowed"):
            assert len(c1.events()) == 1
        else:
            assert len(c1.events()) == 2

        if (
            not self.check_compatibility_flag("unique_calendar_ids")
            and self.cleanup_regime == "post"
        ):
            self._teardownCalendar(cal_id=self.testcal_id)
            self._teardownCalendar(cal_id=self.testcal_id2)

    def testCreateCalendarAndEventFromVobject(self):
        self.skip_on_compatibility_flag("read_only")
        c = self._fixCalendar()

        ## in case the calendar is reused
        cnt = len(c.events())

        # add event from vobject data
        ve1 = vobject.readOne(ev1)
        c.save_event(ve1)
        cnt += 1

        # c.events() should give a full list of events
        events = c.events()
        assert len(events) == cnt

        # This makes no sense, it's a noop.  Perhaps an error
        # should be raised, but as for now, this is simply ignored.
        c.save_event(None)
        assert len(c.events()) == cnt

    def testGetSupportedComponents(self):
        self.skip_on_compatibility_flag("no_supported_components_support")
        c = self._fixCalendar()

        components = c.get_supported_components()
        assert components
        assert "VEVENT" in components

    def testSearchEvent(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_search")
        c = self._fixCalendar()

        num_existing = len(c.events())

        c.save_event(ev1)
        c.save_event(ev3)
        c.save_event(evr)

        ## Search without any parameters should yield everything on calendar
        all_events = c.search()
        if self.check_compatibility_flag("search_needs_comptype"):
            assert len(all_events) <= 3 + num_existing
        else:
            assert len(all_events) == 3 + num_existing

        ## Search with comp_class set to Event should yield all events on calendar
        all_events = c.search(comp_class=Event)
        assert len(all_events) == 3 + num_existing

        ## Search with todo flag set should yield no events
        try:
            no_events = c.search(todo=True)
        except:
            no_events = []
        assert len(no_events) == 0

        ## Date search should be possible
        some_events = c.search(
            comp_class=Event,
            expand=False,
            start=datetime(2006, 7, 13, 13, 0),
            end=datetime(2006, 7, 15, 13, 0),
        )
        if not self.check_compatibility_flag("fastmail_buggy_noexpand_date_search"):
            assert len(some_events) == 1

        ## Search for misc text fields
        ## UID is a special case, supported by almost all servers
        some_events = c.search(
            comp_class=Event, uid="19970901T130000Z-123403@example.com"
        )
        if not self.check_compatibility_flag("text_search_not_working"):
            assert len(some_events) == 1

        ## class
        some_events = c.search(comp_class=Event, class_="CONFIDENTIAL")
        if not self.check_compatibility_flag("text_search_not_working"):
            assert len(some_events) == 1

        ## not defined
        some_events = c.search(comp_class=Event, no_class=True)
        ## ev1, ev3 should be returned
        ## or perhaps not,
        ## ref https://gitlab.com/davical-project/davical/-/issues/281#note_1265743591
        ## PUBLIC is default, so maybe no events should be returned?
        if not self.check_compatibility_flag("isnotdefined_not_working"):
            assert len(some_events) == 2

        some_events = c.search(comp_class=Event, no_category=True)
        ## ev1, ev3 should be returned
        if not self.check_compatibility_flag("isnotdefined_not_working"):
            assert len(some_events) == 2

        some_events = c.search(comp_class=Event, no_dtend=True)
        ## evr should be returned
        if not self.check_compatibility_flag("isnotdefined_not_working"):
            assert len(some_events) == 1

        self.skip_on_compatibility_flag("text_search_not_working")

        ## category
        some_events = c.search(comp_class=Event, category="PERSONAL")
        if not self.check_compatibility_flag("category_search_yields_nothing"):
            assert len(some_events) == 1
        some_events = c.search(comp_class=Event, category="personal")
        if not self.check_compatibility_flag("category_search_yields_nothing"):
            if self.check_compatibility_flag("text_search_is_case_insensitive"):
                assert len(some_events) == 1
            else:
                assert len(some_events) == 0

        ## This is not a very useful search, and it's sort of a client side bug that we allow it at all.
        ## It will not match if categories field is set to "PERSONAL,ANNIVERSARY,SPECIAL OCCASION"
        ## It may not match since the above is to be considered equivalent to the raw data entered.
        some_events = c.search(
            comp_class=Event, category="ANNIVERSARY,PERSONAL,SPECIAL OCCASION"
        )
        assert len(some_events) in (0, 1)
        ## TODO: This is actually a bug. We need to do client side filtering
        some_events = c.search(comp_class=Event, category="PERSON")
        if self.check_compatibility_flag("text_search_is_exact_match_sometimes"):
            assert len(some_events) in (0, 1)
        if self.check_compatibility_flag("text_search_is_exact_match_only"):
            assert len(some_events) == 0
        elif not self.check_compatibility_flag("category_search_yields_nothing"):
            assert len(some_events) == 1

        ## I expect "logical and" when combining category with a date range
        no_events = c.search(
            comp_class=Event,
            category="PERSONAL",
            start=datetime(2006, 7, 13, 13, 0),
            end=datetime(2006, 7, 15, 13, 0),
        )
        if not self.check_compatibility_flag(
            "category_search_yields_nothing"
        ) and not self.check_compatibility_flag("combined_search_not_working"):
            assert len(no_events) == 0
        some_events = c.search(
            comp_class=Event,
            category="PERSONAL",
            start=datetime(1997, 11, 1, 13, 0),
            end=datetime(1997, 11, 3, 13, 0),
        )
        if not self.check_compatibility_flag(
            "category_search_yields_nothing"
        ) and not self.check_compatibility_flag("combined_search_not_working"):
            assert len(some_events) == 1

        some_events = c.search(comp_class=Event, summary="Bastille Day Party")
        assert len(some_events) == 1
        some_events = c.search(comp_class=Event, summary="Bastille Day")
        if self.check_compatibility_flag("text_search_is_exact_match_sometimes"):
            assert len(some_events) in (0, 2)
        elif self.check_compatibility_flag("text_search_is_exact_match_only"):
            assert len(some_events) == 0
        else:
            assert len(some_events) == 2

        ## Even sorting should work out
        all_events = c.search(sort_keys=("summary", "dtstamp"))
        assert len(all_events) == 3
        assert all_events[0].instance.vevent.summary.value == "Bastille Day Jitsi Party"

        ## Sorting by upper case should also wor
        all_events = c.search(sort_keys=("SUMMARY", "DTSTAMP"))
        assert len(all_events) == 3
        assert all_events[0].instance.vevent.summary.value == "Bastille Day Jitsi Party"

        ## Sorting in reverse order should work also
        all_events = c.search(sort_keys=("SUMMARY", "DTSTAMP"), sort_reverse=True)
        assert len(all_events) == 3
        assert all_events[0].instance.vevent.summary.value == "Our Blissful Anniversary"

        ## A more robust check for the sort key
        all_events = c.search(sort_keys=("DTSTART",))
        assert len(all_events) == 3
        assert str(all_events[0].icalendar_component["DTSTART"].dt) < str(
            all_events[1].icalendar_component["DTSTART"].dt
        )
        assert str(all_events[1].icalendar_component["DTSTART"].dt) < str(
            all_events[2].icalendar_component["DTSTART"].dt
        )
        all_events = c.search(sort_keys=("DTSTART",), sort_reverse=True)
        assert str(all_events[0].icalendar_component["DTSTART"].dt) > str(
            all_events[1].icalendar_component["DTSTART"].dt
        )
        assert str(all_events[1].icalendar_component["DTSTART"].dt) > str(
            all_events[2].icalendar_component["DTSTART"].dt
        )

        ## Ref https://github.com/python-caldav/caldav/issues/448
        all_events = c.search(sort_keys=("DTSTART"))
        assert len(all_events) == 3
        assert str(all_events[0].icalendar_component["DTSTART"].dt) < str(
            all_events[1].icalendar_component["DTSTART"].dt
        )
        assert str(all_events[1].icalendar_component["DTSTART"].dt) < str(
            all_events[2].icalendar_component["DTSTART"].dt
        )
        all_events = c.search(sort_keys=("DTSTART"), sort_reverse=True)
        assert str(all_events[0].icalendar_component["DTSTART"].dt) > str(
            all_events[1].icalendar_component["DTSTART"].dt
        )
        assert str(all_events[1].icalendar_component["DTSTART"].dt) > str(
            all_events[2].icalendar_component["DTSTART"].dt
        )

    def testSearchSortTodo(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_todo")
        self.skip_on_compatibility_flag("no_search")
        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])
        pre_todos = c.todos()
        pre_todo_uid_map = {x.icalendar_component["uid"] for x in pre_todos}
        cleanse = lambda tasks: [
            x for x in tasks if x.icalendar_component["uid"] not in pre_todo_uid_map
        ]
        t1 = c.save_todo(
            summary="1 task overdue",
            due=date(2022, 12, 12),
            dtstart=date(2022, 10, 11),
            uid="test1",
        )
        t2 = c.save_todo(
            summary="2 task future",
            due=datetime.now() + timedelta(hours=15),
            dtstart=datetime.now() + timedelta(minutes=15),
            uid="test2",
        )
        t3 = c.save_todo(
            summary="3 task future due",
            due=datetime.now() + timedelta(hours=15),
            dtstart=datetime(2022, 12, 11, 10, 9, 8),
            uid="test3",
        )
        t4 = c.save_todo(
            summary="4 task priority is set to nine which is the lowest",
            priority=9,
            uid="test4",
        )
        t5 = c.save_todo(
            summary="5 task status is set to COMPLETED and this will disappear from the ordinary todo search",
            status="COMPLETED",
            uid="test5",
        )
        t6 = c.save_todo(
            summary="6 task has categories",
            categories="home,garden,sunshine",
            uid="test6",
        )

        def check_order(tasks, order):
            assert [str(x.icalendar_component["uid"]) for x in tasks] == [
                "test" + str(x) for x in order
            ]

        all_tasks = cleanse(c.search(todo=True, sort_keys=("uid",)))
        check_order(all_tasks, (1, 2, 3, 4, 6))

        all_tasks = cleanse(c.search(sort_keys=("summary",)))
        check_order(all_tasks, (1, 2, 3, 4, 5, 6))

        all_tasks = cleanse(
            c.search(
                sort_keys=(
                    "isnt_overdue",
                    "categories",
                    "dtstart",
                    "priority",
                    "status",
                )
            )
        )
        ## This is difficult ...
        ## * 1 is the only one that is overdue, and False sorts before True, so 1 comes first
        ## * categories, empty string sorts before a non-empty string, so 6 is at the end of the list
        ## So we have 2-5 still to worry about ...
        ## * dtstart - default is "long ago", so 4,5 or 5,4 should be first, followed by 3,2
        ## * priority - default is 0, so 5 comes before 4
        check_order(all_tasks, (1, 5, 4, 3, 2, 6))

    def testSearchTodos(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_todo")
        self.skip_on_compatibility_flag("no_search")
        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])

        pre_cnt = len(c.todos())

        t1 = c.save_todo(todo)
        t2 = c.save_todo(todo2)
        t3 = c.save_todo(todo3)
        t4 = c.save_todo(todo4)
        t5 = c.save_todo(todo5)
        t6 = c.save_todo(todo6)

        ## Search without any parameters should yield everything on calendar
        all_todos = c.search()
        if self.check_compatibility_flag("search_needs_comptype"):
            assert len(all_todos) <= 6 + pre_cnt
        else:
            assert len(all_todos) == 6 + pre_cnt

        ## Search with comp_class set to Event should yield all events on calendar
        all_todos = c.search(comp_class=Event)
        assert len(all_todos) == 0 + pre_cnt

        ## Search with todo flag set should yield all 6 tasks
        ## (Except, if the calendar server does not support is-not-defined very
        ## well, perhaps only 3 will be returned - see
        ## https://gitlab.com/davical-project/davical/-/issues/281 )
        all_todos = c.search(todo=True)
        if self.check_compatibility_flag("isnotdefined_not_working"):
            assert len(all_todos) - pre_cnt in (3, 6)
        else:
            assert len(all_todos) == 6 + pre_cnt

        ## Search for misc text fields
        ## UID is a special case, supported by almost all servers
        some_todos = c.search(comp_class=Todo, uid="19970901T130000Z-123404@host.com")
        if not self.check_compatibility_flag("text_search_not_working"):
            assert len(some_todos) == 1 + pre_cnt

        ## class ... hm, all 6 example todos are 'CONFIDENTIAL' ...
        some_todos = c.search(comp_class=Todo, class_="CONFIDENTIAL")
        if not self.check_compatibility_flag("text_search_not_working"):
            assert len(some_todos) == 6 + pre_cnt

        ## category
        ## Too much copying of the examples ...
        some_todos = c.search(comp_class=Todo, category="FINANCE")
        if not self.check_compatibility_flag(
            "category_search_yields_nothing"
        ) and not self.check_compatibility_flag("text_search_not_working"):
            assert len(some_todos) == 6 + pre_cnt
        some_todos = c.search(comp_class=Todo, category="finance")
        if not self.check_compatibility_flag(
            "category_search_yields_nothing"
        ) and not self.check_compatibility_flag("text_search_not_working"):
            if self.check_compatibility_flag("text_search_is_case_insensitive"):
                assert len(some_todos) == 6 + pre_cnt
            else:
                assert len(some_todos) == 0 + pre_cnt

        ## This is not a very useful search, and it's sort of a client side bug that we allow it at all.
        ## It will not match if categories field is set to "PERSONAL,ANNIVERSARY,SPECIAL OCCASION"
        ## It may not match since the above is to be considered equivalent to the raw data entered.
        some_todos = c.search(comp_class=Todo, category="FAMILY,FINANCE")
        if not self.check_compatibility_flag("text_search_not_working"):
            assert len(some_todos) - pre_cnt in (0, 6)
        ## TODO: We should consider to do client side filtering to ensure exact
        ## match only on components having MIL as a category (and not FAMILY)
        some_todos = c.search(comp_class=Todo, category="MIL")
        if self.check_compatibility_flag("text_search_is_exact_match_sometimes"):
            assert len(some_todos) - pre_cnt in (0, 6)
        elif self.check_compatibility_flag("text_search_is_exact_match_only"):
            assert len(some_todos) - pre_cnt == 0
        elif not self.check_compatibility_flag(
            "category_search_yields_nothing"
        ) and not self.check_compatibility_flag("text_search_not_working"):
            ## This is the correct thing, according to the letter of the RFC
            assert len(some_todos) - pre_cnt == 6

        ## completing events, and it should not show up anymore
        t3.complete()
        t5.complete()
        t6.complete()

        some_todos = c.search(todo=True)
        assert len(some_todos) == 3 + pre_cnt

        ## unless we specifically ask for completed tasks
        all_todos = c.search(todo=True, include_completed=True)
        assert len(all_todos) == 6 + pre_cnt

    def testWrongPassword(self):
        if (
            not "password" in self.server_params
            or not self.server_params["password"]
            or self.server_params["password"] == "any-password-seems-to-work"
        ):
            pytest.skip(
                "Testing with wrong password skipped as calendar server does not require a password"
            )
        connect_params = self.server_params.copy()
        for delme in ("setup", "teardown", "name"):
            if delme in connect_params:
                connect_params.pop(delme)
        connect_params["password"] = (
            codecs.encode(connect_params["password"], "rot13") + "!"
        )
        with pytest.raises(error.AuthorizationError):
            client(**connect_params).principal()

    def testCreateChildParent(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_relships")
        self.skip_on_compatibility_flag("object_by_uid_is_broken")
        c = self._fixCalendar(supported_calendar_component_set=["VEVENT"])
        parent = c.save_event(
            dtstart=datetime(2022, 12, 26, 19, 15),
            dtend=datetime(2022, 12, 26, 20, 00),
            summary="this is a parent event test",
            uid="ctuid1",
        )
        child = c.save_event(
            dtstart=datetime(2022, 12, 26, 19, 17),
            dtend=datetime(2022, 12, 26, 20, 00),
            summary="this is a child event test",
            parent=[parent.id],
            uid="ctuid2",
        )
        grandparent = c.save_event(
            dtstart=datetime(2022, 12, 26, 19, 00),
            dtend=datetime(2022, 12, 26, 20, 00),
            summary="this is a grandparent event test",
            child=[parent.id],
            uid="ctuid3",
        )

        parent_ = c.event_by_uid(parent.id)
        child_ = c.event_by_uid(child.id)
        grandparent_ = c.event_by_uid(grandparent.id)

        rt = grandparent_.icalendar_component["RELATED-TO"]
        if isinstance(rt, list):
            assert len(rt) == 1
            rt = rt[0]
        assert rt == parent.id
        assert rt.params["RELTYPE"] == "CHILD"
        rt = parent_.icalendar_component["RELATED-TO"]
        assert len(rt) == 2
        assert set([str(rt[0]), str(rt[1])]) == set([grandparent.id, child.id])
        assert set([rt[0].params["RELTYPE"], rt[1].params["RELTYPE"]]) == set(
            ["CHILD", "PARENT"]
        )
        rt = child_.icalendar_component["RELATED-TO"]
        if isinstance(rt, list):
            assert len(rt) == 1
            rt = rt[0]
        assert rt == parent.id
        assert rt.params["RELTYPE"] == "PARENT"

        foo = parent_.get_relatives(reltypes={"PARENT"})
        assert len(foo) == 1
        assert len(foo["PARENT"]) == 1
        assert [list(foo["PARENT"])[0].icalendar_component["UID"] == grandparent.id]
        foo = parent_.get_relatives(reltypes={"CHILD"})
        assert len(foo) == 1
        assert len(foo["CHILD"]) == 1
        assert [list(foo["CHILD"])[0].icalendar_component["UID"] == child.id]
        foo = parent_.get_relatives(reltypes={"CHILD", "PARENT"})
        assert len(foo) == 2
        assert len(foo["CHILD"]) == 1
        assert len(foo["PARENT"]) == 1
        foo = parent_.get_relatives(relfilter=lambda x: x.params.get("GAP"))

        ## verify the check_reverse_relations method (TODO: move to a separate test)
        assert parent_.check_reverse_relations() == []
        assert child_.check_reverse_relations() == []
        assert grandparent_.check_reverse_relations() == []

        ## My grandchild is also my child ... that sounds fishy
        grandparent_.set_relation(child, reltype="CHILD", set_reverse=False)

        ## The check_reverse should tell that something is amiss
        missing_parent = grandparent_.check_reverse_relations()
        assert len(missing_parent) == 1
        assert missing_parent[0][0].icalendar_component["uid"] == "ctuid2"
        assert missing_parent[0][1] == "PARENT"
        ## But only when run on the grandparent.  The child is blissfully
        ## unaware who the second parent is (even if reloading it).
        child_.load()
        assert child_.check_reverse_relations() == []

        ## We should be able to fix the missing parent
        grandparent_.fix_reverse_relations()
        assert not grandparent_.check_reverse_relations()

        ## TODO:
        ## This does not work out.  A relation to some object that is not on
        ## the calendar is not flagged - but perhaps it shouldn't be flagged?
        # child.delete()
        # assert parent_.check_reverse_relations()

    def testSetDue(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_todo")

        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])

        utc = timezone.utc

        some_todo = c.save_todo(
            dtstart=datetime(2022, 12, 26, 19, 15, tzinfo=utc),
            due=datetime(2022, 12, 26, 20, 00, tzinfo=utc),
            summary="Some task",
            uid="ctuid1",
        )

        ## setting the due should ... set the due (surprise, surprise)
        some_todo.set_due(datetime(2022, 12, 26, 20, 10, tzinfo=utc))
        assert some_todo.icalendar_component["DUE"].dt == datetime(
            2022, 12, 26, 20, 10, tzinfo=utc
        )
        assert some_todo.icalendar_component["DTSTART"].dt == datetime(
            2022, 12, 26, 19, 15, tzinfo=utc
        )

        ## move_dtstart causes the duration to be unchanged
        some_todo.set_due(datetime(2022, 12, 26, 20, 20, tzinfo=utc), move_dtstart=True)
        assert some_todo.icalendar_component["DUE"].dt == datetime(
            2022, 12, 26, 20, 20, tzinfo=utc
        )
        assert some_todo.icalendar_component["DTSTART"].dt == datetime(
            2022, 12, 26, 19, 25, tzinfo=utc
        )

        ## This task has duration set rather than due.  Due should be implied to be 19:30.
        some_other_todo = c.save_todo(
            dtstart=datetime(2022, 12, 26, 19, 15, tzinfo=utc),
            duration=timedelta(minutes=15),
            summary="Some other task",
            uid="ctuid2",
        )
        some_other_todo.set_due(
            datetime(2022, 12, 26, 19, 45, tzinfo=utc), move_dtstart=True
        )
        assert some_other_todo.icalendar_component["DUE"].dt == datetime(
            2022, 12, 26, 19, 45, tzinfo=utc
        )
        assert some_other_todo.icalendar_component["DTSTART"].dt == datetime(
            2022, 12, 26, 19, 30, tzinfo=utc
        )

        some_todo.save()

        self.skip_on_compatibility_flag("no_relships")
        self.skip_on_compatibility_flag("object_by_uid_is_broken")
        parent = c.save_todo(
            dtstart=datetime(2022, 12, 26, 19, 00, tzinfo=utc),
            dtend=datetime(2022, 12, 26, 21, 00, tzinfo=utc),
            summary="this is a parent test task",
            uid="ctuid3",
            child=[some_todo.id],
        )

        assert not parent.check_reverse_relations()

        ## The above updates the some_todo object on the server side, but the local object is not
        ## updated ... until we reload it
        some_todo.load()

        ## This should work out (set the children due to some time before the parents due)
        some_todo.set_due(
            datetime(2022, 12, 26, 20, 30, tzinfo=utc),
            move_dtstart=True,
            check_dependent=True,
        )
        assert some_todo.icalendar_component["DUE"].dt == datetime(
            2022, 12, 26, 20, 30, tzinfo=utc
        )
        assert some_todo.icalendar_component["DTSTART"].dt == datetime(
            2022, 12, 26, 19, 35, tzinfo=utc
        )

        ## This should not work out (set the children due to some time before the parents due)
        with pytest.raises(error.ConsistencyError):
            some_todo.set_due(
                datetime(2022, 12, 26, 21, 30, tzinfo=utc),
                move_dtstart=True,
                check_dependent=True,
            )

        child = c.save_todo(
            dtstart=datetime(2022, 12, 26, 19, 45),
            due=datetime(2022, 12, 26, 19, 55),
            summary="this is a test child task",
            uid="ctuid4",
            parent=[some_todo.id],
        )

        ## This should still work out (set the children due to some time before the parents due)
        ## (The fact that we now have a child does not affect it anyhow)
        some_todo.set_due(
            datetime(2022, 12, 26, 20, 31, tzinfo=utc),
            move_dtstart=True,
            check_dependent=True,
        )
        assert some_todo.icalendar_component["DUE"].dt == datetime(
            2022, 12, 26, 20, 31, tzinfo=utc
        )
        assert some_todo.icalendar_component["DTSTART"].dt == datetime(
            2022, 12, 26, 19, 36, tzinfo=utc
        )

    def testCreateJournalListAndJournalEntry(self):
        """
        This test demonstrates the support for journals.
        * It will create a journal list
        * It will add some journal entries to it
        * It will list out all journal entries
        """
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_journal")
        c = self._fixCalendar(supported_calendar_component_set=["VJOURNAL"])
        j1 = c.save_journal(journal)
        journals = c.journals()
        assert len(journals) == 1
        self.skip_on_compatibility_flag("object_by_uid_is_broken")
        j1_ = c.journal_by_uid(j1.id)
        j1_.icalendar_instance
        journals[0].icalendar_instance
        assert j1_.data == journals[0].data
        j2 = c.save_journal(
            dtstart=date(2011, 11, 11),
            summary="A childbirth in a hospital in Kupchino",
            description="A quick birth, in the middle of the night",
            uid="ctuid1",
        )
        assert len(c.journals()) == 2
        assert len(c.search(journal=True)) == 2
        todos = c.todos()
        events = c.events()
        assert todos + events == []

    def testCreateTaskListAndTodo(self):
        """
        This test demonstrates the support for task lists.
        * It will create a "task list"
        * It will add a task to it
        * Verify the cal.todos() method
        * Verify that cal.events() method returns nothing
        """
        self.skip_on_compatibility_flag("read_only")

        # bedeworks and google calendar and some others does not support VTODO
        self.skip_on_compatibility_flag("no_todo")

        # For most servers (notable exception Zimbra), it's
        # possible to create a calendar and add todo-items to it.
        # Zimbra has separate calendars and task lists, and it's not
        # allowed to put TODO-tasks into the calendar.  We need to
        # tell Zimbra that the new "calendar" is a task list.  This
        # is done though the supported_calendar_component_set
        # property - hence the extra parameter here:
        logging.info("Creating calendar Yep for tasks")
        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])

        # add todo-item
        logging.info("Adding todo item to calendar Yep")
        t1 = c.save_todo(todo)
        assert t1.id == "20070313T123432Z-456553@example.com"

        # c.todos() should give a full list of todo items
        logging.info("Fetching the full list of todo items (should be one)")
        todos = c.todos()
        todos2 = c.todos(include_completed=True)
        assert len(todos) == 1
        assert len(todos2) == 1

        t3 = c.save_todo(
            summary="mop the floor", categories=["housework"], priority=4, uid="ctuid1"
        )
        assert len(c.todos()) == 2

        # adding a todo without a UID, it should also work (library will add the missing UID)
        t7 = c.save_todo(todo7)
        logging.info("Fetching the todos (should be three)")
        todos = c.todos()

        logging.info("Fetching the events (should be none)")
        # c.events() should NOT return todo-items
        events = c.events()

        t7.delete()

        ## Delayed asserts ... this test is fragile, since todo7 is without
        ## an uid it may not be covered by the automatic cleanup procedures
        ## in the test framework.
        assert len(todos) == 3
        assert len(events) == 0
        assert len(c.todos()) == 2

    def testTodos(self):
        """
        This test will exercise the cal.todos() method,
        and in particular the sort_keys attribute.
        * It will list out all pending tasks, sorted by due date
        * It will list out all pending tasks, sorted by priority
        """
        self.skip_on_compatibility_flag("read_only")
        # Not all server implementations have support for VTODO
        self.skip_on_compatibility_flag("no_todo")
        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])

        # add todo-item
        t1 = c.save_todo(todo)
        t2 = c.save_todo(todo2)
        t4 = c.save_todo(todo4)

        todos = c.todos()
        assert len(todos) == 3

        def uids(lst):
            return [x.instance.vtodo.uid for x in lst]

        ## Default sort order is (due, priority).
        assert uids(todos) == uids([t2, t1, t4])

        todos = c.todos(sort_keys=("priority",))
        ## sort_key is considered to be a legacy parameter,
        ## but should work at least until 1.0
        todos2 = c.todos(sort_key="priority")

        def pri(lst):
            return [
                x.instance.vtodo.priority.value
                for x in lst
                if hasattr(x.instance.vtodo, "priority")
            ]

        assert pri(todos) == pri([t4, t2])
        assert pri(todos2) == pri([t4, t2])

        todos = c.todos(
            sort_keys=(
                "summary",
                "priority",
            )
        )
        assert uids(todos) == uids([t4, t2, t1])

        ## str of CalendarObjectResource is slightly inconsistent compared to
        ## the str of Calendar objects, as the class name is included.  Perhaps
        ## it should be removed, hence no assertions on that.
        ## (the statements below is mostly to exercise the __str__ and __repr__)
        assert str(todos[0].url) in str(todos[0])
        assert str(todos[0].url) in repr(todos[0])
        assert "Todo" in repr(todos[0])

    def testTodoDatesearch(self):
        """
        Let's see how the date search method works for todo events
        """
        self.skip_on_compatibility_flag("read_only")
        # bedeworks does not support VTODO
        self.skip_on_compatibility_flag("no_todo")
        self.skip_on_compatibility_flag("no_todo_datesearch")
        self.skip_on_compatibility_flag("no_search")
        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])

        # add todo-item
        t1 = c.save_todo(todo)
        t2 = c.save_todo(todo2)
        t3 = c.save_todo(todo3)
        t4 = c.save_todo(todo4)
        t5 = c.save_todo(todo5)
        t6 = c.save_todo(todo6)
        todos = c.todos()
        if self.check_compatibility_flag("isnotdefined_not_working"):
            assert len(todos) in (3, 6)
        else:
            assert len(todos) == 6

        notodos = c.date_search(  # default compfilter is events
            start=datetime(1997, 4, 14), end=datetime(2015, 5, 14), expand=False
        )
        assert not notodos

        # Now, this is interesting.
        # t1 has due set but not dtstart set
        # t2 and t3 has dtstart and due set
        # t4 has neither dtstart nor due set.
        # t5 has dtstart and due set prior to the search window
        # t6 has dtstart and due set prior to the search window, but is yearly recurring.
        # What will a date search yield?
        todos1 = c.date_search(
            start=datetime(1997, 4, 14),
            end=datetime(2015, 5, 14),
            compfilter="VTODO",
            expand=True,
        )
        todos2 = c.search(
            start=datetime(1997, 4, 14),
            end=datetime(2015, 5, 14),
            todo=True,
            expand=True,
            split_expanded=False,
            include_completed=True,
        )
        todos3 = c.search(
            start=datetime(1997, 4, 14),
            end=datetime(2015, 5, 14),
            todo=True,
            expand="client",
            split_expanded=False,
            include_completed=True,
        )
        todos4 = c.search(
            start=datetime(1997, 4, 14),
            end=datetime(2015, 5, 14),
            todo=True,
            expand="client",
            split_expanded=False,
            include_completed=True,
        )
        # The RFCs are pretty clear on this.  rfc5545 states:

        # A "VTODO" calendar component without the "DTSTART" and "DUE" (or
        # "DURATION") properties specifies a to-do that will be associated
        # with each successive calendar date, until it is completed.

        # and RFC4791, section 9.9 also says that events without
        # dtstart or due should be counted.  The expanded yearly event
        # should be returned as one object with multiple BEGIN:VEVENT
        # and DTSTART lines.

        # Hence a compliant server should chuck out all the todos except t5.
        # Not all servers perform according to (my interpretation of) the RFC.
        foo = 5
        if self.check_compatibility_flag(
            "no_recurring"
        ) or self.check_compatibility_flag("no_recurring_todo"):
            foo -= 1  ## t6 will not be returned
        if self.check_compatibility_flag(
            "vtodo_datesearch_nodtstart_task_is_skipped"
        ) or self.check_compatibility_flag(
            "vtodo_datesearch_nodtstart_task_is_skipped_in_closed_date_range"
        ):
            foo -= 2  ## t1 and t4 not returned
        elif self.check_compatibility_flag("vtodo_datesearch_notime_task_is_skipped"):
            foo -= 1  ## t4 not returned
        assert len(todos1) == foo
        assert len(todos2) == foo

        ## verify that "expand" works
        if not self.check_compatibility_flag(
            "no_recurring"
        ) and not self.check_compatibility_flag("no_recurring_todo"):
            ## todo1 and todo2 should be the same (todo1 using legacy method)
            ## todo1 and todo2 tries doing server side expand, with fallback
            ## to client side expand
            if not self.check_compatibility_flag("broken_expand"):
                assert (
                    len([x for x in todos1 if "DTSTART:20020415T1330" in x.data]) == 1
                )
                assert (
                    len([x for x in todos2 if "DTSTART:20020415T1330" in x.data]) == 1
                )
                if not self.check_compatibility_flag("no_expand"):
                    assert (
                        len([x for x in todos4 if "DTSTART:20020415T1330" in x.data])
                        == 1
                    )
            ## todo3 is client side expand, should always work
            assert len([x for x in todos3 if "DTSTART:20020415T1330" in x.data]) == 1
            ## todo4 is server side expand, may work dependent on server

        ## exercise the default for expand (maybe -> False for open-ended search)
        todos1 = c.date_search(start=datetime(2025, 4, 14), compfilter="VTODO")
        todos2 = c.search(
            start=datetime(2025, 4, 14), todo=True, include_completed=True
        )
        todos3 = c.search(start=datetime(2025, 4, 14), todo=True)

        assert isinstance(todos1[0], Todo)
        assert isinstance(todos2[0], Todo)
        if not self.check_compatibility_flag("combined_search_not_working"):
            assert isinstance(todos3[0], Todo)

        ## * t6 should be returned, as it's a yearly task spanning over 2025
        ## * t1 should probably be returned, as it has no due date set and hence
        ## has an infinite duration.
        ## * t4 should probably be returned, as it has no dtstart nor due and
        ##  hence is also considered to span over infinite time
        urls_found = [x.url for x in todos1]
        urls_found2 = [x.url for x in todos1]
        assert urls_found == urls_found2
        if not (
            self.check_compatibility_flag("no_recurring")
            or self.check_compatibility_flag("no_recurring_todo")
        ):
            urls_found.remove(t6.url)
        if not self.check_compatibility_flag(
            "vtodo_datesearch_nodtstart_task_is_skipped"
        ) and not self.check_compatibility_flag(
            "vtodo_datesearch_notime_task_is_skipped"
        ):
            urls_found.remove(t4.url)
        if self.check_compatibility_flag("vtodo_no_due_infinite_duration"):
            urls_found.remove(t1.url)
        ## everything should be popped from urls_found by now
        assert len(urls_found) == 0

        assert len([x for x in todos1 if "DTSTART:20270415T1330" in x.data]) == 0
        assert len([x for x in todos2 if "DTSTART:20270415T1330" in x.data]) == 0

        # TODO: prod the caldav server implementers about the RFC
        # breakages.

    def testTodoCompletion(self):
        """
        Will check that todo-items can be completed and deleted
        """
        self.skip_on_compatibility_flag("read_only")
        # not all caldav servers support VTODO
        self.skip_on_compatibility_flag("no_todo")
        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])

        # add todo-items
        t1 = c.save_todo(todo)
        t2 = c.save_todo(todo2)
        t3 = c.save_todo(todo3, status="NEEDS-ACTION")

        # There are now three todo-items at the calendar
        todos = c.todos()
        assert len(todos) == 3

        # Complete one of them
        t3.complete()

        # There are now two todo-items at the calendar
        todos = c.todos()
        assert len(todos) == 2

        # The historic todo-item can still be accessed
        todos = c.todos(include_completed=True)
        assert len(todos) == 3
        if not self.check_compatibility_flag("object_by_uid_is_broken"):
            t3_ = c.todo_by_uid(t3.id)
            assert t3_.instance.vtodo.summary == t3.instance.vtodo.summary
            assert t3_.instance.vtodo.uid == t3.instance.vtodo.uid
            assert t3_.instance.vtodo.dtstart == t3.instance.vtodo.dtstart

        t2.delete()

        # ... the deleted one is gone ...
        if not self.check_compatibility_flag("event_by_url_is_broken"):
            todos = c.todos(include_completed=True)
            assert len(todos) == 2

        # date search should not include completed events ... hum.
        # TODO, fixme.
        # todos = c.date_search(
        #     start=datetime(1990, 4, 14), end=datetime(2015,5,14),
        #     compfilter='VTODO', hide_completed_todos=True)
        # assert len(todos) == 1

    def testTodoRecurringCompleteSafe(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_todo")
        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])
        t6 = c.save_todo(todo6, status="NEEDS-ACTION")
        if not self.check_compatibility_flag("rrule_takes_no_count"):
            t8 = c.save_todo(todo8)
        if not self.check_compatibility_flag("rrule_takes_no_count"):
            assert len(c.todos()) == 2
        else:
            assert len(c.todos()) == 1
        t6.complete(handle_rrule=True, rrule_mode="safe")
        if self.check_compatibility_flag("rrule_takes_no_count"):
            assert len(c.todos()) == 1
            assert len(c.todos(include_completed=True)) == 2
            c.todos()[0].delete()
        self.skip_on_compatibility_flag("rrule_takes_no_count")
        assert len(c.todos()) == 2
        assert len(c.todos(include_completed=True)) == 3
        t8.complete(handle_rrule=True, rrule_mode="safe")
        todos = c.todos()
        assert len(todos) == 2
        t8.complete(handle_rrule=True, rrule_mode="safe")
        t8.complete(handle_rrule=True, rrule_mode="safe")
        assert len(c.todos()) == 1
        assert len(c.todos(include_completed=True)) == 5
        [x.delete() for x in c.todos(include_completed=True)]

    def testTodoRecurringCompleteThisandfuture(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_todo")
        c = self._fixCalendar(supported_calendar_component_set=["VTODO"])
        t6 = c.save_todo(todo6, status="NEEDS-ACTION")
        if not self.check_compatibility_flag("rrule_takes_no_count"):
            t8 = c.save_todo(todo8)
        if not self.check_compatibility_flag("rrule_takes_no_count"):
            assert len(c.todos()) == 2
        else:
            assert len(c.todos()) == 1
        t6.complete(handle_rrule=True, rrule_mode="thisandfuture")
        all_todos = c.todos(include_completed=True)
        if self.check_compatibility_flag("rrule_takes_no_count"):
            assert len(c.todos()) == 1
            assert len(all_todos) == 1
        self.skip_on_compatibility_flag("rrule_takes_no_count")
        assert len(c.todos()) == 2
        assert len(all_todos) == 2
        # assert sum([len(x.icalendar_instance.subcomponents) for x in all_todos]) == 5
        t8.complete(handle_rrule=True, rrule_mode="thisandfuture")
        assert len(c.todos()) == 2
        t8.complete(handle_rrule=True, rrule_mode="thisandfuture")
        t8.complete(handle_rrule=True, rrule_mode="thisandfuture")
        assert len(c.todos()) == 1

    def testUtf8Event(self):
        self.skip_on_compatibility_flag("read_only")
        # TODO: what's the difference between this and testUnicodeEvent?
        # TODO: split up in creating a calendar with non-ascii name
        # and an event with non-ascii description
        self.skip_on_compatibility_flag("no_mkcalendar")
        if not self.check_compatibility_flag(
            "unique_calendar_ids"
        ) and self.cleanup_regime in ("light", "pre"):
            self._teardownCalendar(cal_id=self.testcal_id)

        c = self._fixCalendar(name="Yølp", cal_id=self.testcal_id)

        # add event
        e1 = c.save_event(
            ev1.replace("Bastille Day Party", "Bringebærsyltetøyfestival")
        )

        # fetch it back
        events = c.events()

        # no todos should be added
        if not self.check_compatibility_flag("no_todo"):
            todos = c.todos()
            assert len(todos) == 0

        # COMPATIBILITY PROBLEM - todo, look more into it
        if "zimbra" not in str(c.url):
            assert len(events) == 1

        if (
            not self.check_compatibility_flag("unique_calendar_ids")
            and self.cleanup_regime == "post"
        ):
            self._teardownCalendar(cal_id=self.testcal_id)

    def testUnicodeEvent(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_mkcalendar")
        if not self.check_compatibility_flag(
            "unique_calendar_ids"
        ) and self.cleanup_regime in ("light", "pre"):
            self._teardownCalendar(cal_id=self.testcal_id)
        c = self._fixCalendar(name="Yølp", cal_id=self.testcal_id)

        # add event
        e1 = c.save_event(
            to_str(ev1.replace("Bastille Day Party", "Bringebærsyltetøyfestival"))
        )

        # c.events() should give a full list of events
        events = c.events()

        # COMPATIBILITY PROBLEM - todo, look more into it
        if "zimbra" not in str(c.url):
            assert len(events) == 1

    def testSetCalendarProperties(self):
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_displayname")
        self.skip_on_compatibility_flag("no_delete_calendar")

        c = self._fixCalendar()
        assert c.url is not None

        ## TODO: there are more things in this test that
        ## should be run even if mkcalendar is not available.
        self.skip_on_compatibility_flag("no_mkcalendar")

        props = c.get_properties(
            [
                dav.DisplayName(),
            ]
        )
        assert "Yep" == props[dav.DisplayName.tag]

        # Creating a new calendar with different ID but with existing name
        # TODO: why do we do this?
        if not self.check_compatibility_flag(
            "unique_calendar_ids"
        ) and self.cleanup_regime in ("light", "pre"):
            self._teardownCalendar(cal_id=self.testcal_id2)
        cc = self._fixCalendar(name="Yep", cal_id=self.testcal_id2)
        try:
            cc.delete()
        except error.DeleteError:
            if not self.check_compatibility_flag(
                "no_delete_calendar"
            ) or self.check_compatibility_flag("unique_calendar_ids"):
                raise

        c.set_properties(
            [
                dav.DisplayName("hooray"),
            ]
        )
        props = c.get_properties(
            [
                dav.DisplayName(),
            ]
        )
        assert props[dav.DisplayName.tag] == "hooray"

        ## calendar color and calendar order are extra properties not
        ## described by RFC5545, but anyway supported by quite some
        ## server implementations
        if self.check_compatibility_flag("calendar_color"):
            props = c.get_properties(
                [
                    ical.CalendarColor(),
                ]
            )
            assert props[ical.CalendarColor.tag] != "sort of blueish"
            c.set_properties(
                [
                    ical.CalendarColor("blue"),
                ]
            )
            props = c.get_properties(
                [
                    ical.CalendarColor(),
                ]
            )
            assert props[ical.CalendarColor.tag] == "blue"
        if self.check_compatibility_flag("calendar_order"):
            props = c.get_properties(
                [
                    ical.CalendarOrder(),
                ]
            )
            assert props[ical.CalendarOrder.tag] != "-434"
            c.set_properties(
                [
                    ical.CalendarOrder("12"),
                ]
            )
            props = c.get_properties(
                [
                    ical.CalendarOrder(),
                ]
            )
            assert props[ical.CalendarOrder.tag] == "12"

    def testLookupEvent(self):
        """
        Makes sure we can add events and look them up by URL and ID
        """
        self.skip_on_compatibility_flag("read_only")
        # Create calendar
        c = self._fixCalendar()
        assert c.url is not None

        # add event
        e1 = c.save_event(ev1)
        assert e1.url is not None

        # Verify that we can look it up, both by URL and by ID
        if not self.check_compatibility_flag("event_by_url_is_broken"):
            e2 = c.event_by_url(e1.url)
            assert e2.instance.vevent.uid == e1.instance.vevent.uid
            assert e2.url == e1.url
        if not self.check_compatibility_flag("object_by_uid_is_broken"):
            e3 = c.event_by_uid("20010712T182145Z-123401@example.com")
            assert e3.instance.vevent.uid == e1.instance.vevent.uid
            assert e3.url == e1.url

        # Knowing the URL of an event, we should be able to get to it
        # without going through a calendar object
        if not self.check_compatibility_flag("event_by_url_is_broken"):
            e4 = Event(client=self.caldav, url=e1.url)
            e4.load()
            assert e4.instance.vevent.uid == e1.instance.vevent.uid

        with pytest.raises(error.NotFoundError):
            c.event_by_uid("0")
        c.save_event(evr)
        with pytest.raises(error.NotFoundError):
            c.event_by_uid("0")

    def testCreateOverwriteDeleteEvent(self):
        """
        Makes sure we can add events and delete them
        """
        self.skip_on_compatibility_flag("read_only")
        # Create calendar
        c = self._fixCalendar()
        assert c.url is not None

        # attempts on updating/overwriting a non-existing event should fail (unless object_by_uid_is_broken):
        if not self.check_compatibility_flag("object_by_uid_is_broken"):
            with pytest.raises(error.ConsistencyError):
                c.save_event(ev1, no_create=True)

        # no_create and no_overwrite is mutually exclusive, this will always
        # raise an error (unless the ical given is blank)
        with pytest.raises(error.ConsistencyError):
            c.save_event(ev1, no_create=True, no_overwrite=True)

        # add event
        e1 = c.save_event(ev1)

        todo_ok = not self.check_compatibility_flag(
            "no_todo"
        ) and not self.check_compatibility_flag("no_events_and_tasks_on_same_calendar")
        if todo_ok:
            t1 = c.save_todo(todo)
        assert e1.url is not None
        if todo_ok:
            assert t1.url is not None
        if not self.check_compatibility_flag("event_by_url_is_broken"):
            assert c.event_by_url(e1.url).url == e1.url
        if not self.check_compatibility_flag("object_by_uid_is_broken"):
            assert c.event_by_uid(e1.id).url == e1.url

        ## no_create will not work unless object_by_uid works
        no_create = not self.check_compatibility_flag("object_by_uid_is_broken")

        ## add same event again.  As it has same uid, it should be overwritten
        ## (but some calendars may throw a "409 Conflict")
        if not self.check_compatibility_flag("no_overwrite"):
            e2 = c.save_event(ev1)
            if todo_ok:
                t2 = c.save_todo(todo)

            ## add same event with "no_create".  Should work like a charm.
            e2 = c.save_event(ev1, no_create=no_create)
            if todo_ok:
                t2 = c.save_todo(todo, no_create=no_create)

            ## this should also work.
            e2.instance.vevent.summary.value = e2.instance.vevent.summary.value + "!"
            e2.save(no_create=no_create)

            if todo_ok:
                t2.instance.vtodo.summary.value = t2.instance.vtodo.summary.value + "!"
                t2.save(no_create=no_create)

            if not self.check_compatibility_flag("event_by_url_is_broken"):
                e3 = c.event_by_url(e1.url)
                assert e3.instance.vevent.summary.value == "Bastille Day Party!"

        ## "no_overwrite" should throw a ConsistencyError.  But it depends on object_by_uid.
        if not self.check_compatibility_flag("object_by_uid_is_broken"):
            with pytest.raises(error.ConsistencyError):
                c.save_event(ev1, no_overwrite=True)
            if todo_ok:
                with pytest.raises(error.ConsistencyError):
                    c.save_todo(todo, no_overwrite=True)

        # delete event
        e1.delete()
        if todo_ok:
            t1.delete()

        if self.check_compatibility_flag("non_existing_raises_other"):
            expected_error = error.DAVError
        else:
            expected_error = error.NotFoundError

        # Verify that we can't look it up, both by URL and by ID
        with pytest.raises(self._notFound()):
            c.event_by_url(e1.url)
        if not self.check_compatibility_flag("no_overwrite"):
            with pytest.raises(self._notFound()):
                c.event_by_url(e2.url)
        if not self.check_compatibility_flag("event_by_url_is_broken"):
            with pytest.raises(error.NotFoundError):
                c.event_by_uid("20010712T182145Z-123401@example.com")

    def testDateSearchAndFreeBusy(self):
        """
        Verifies that date search works with a non-recurring event
        Also verifies that it's possible to change a date of a
        non-recurring event
        """
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_search")
        # Create calendar, add event ...
        c = self._fixCalendar()
        assert c.url is not None
        e = c.save_event(ev1)

        ## just a sanity check to increase coverage (ref
        ## https://github.com/python-caldav/caldav/issues/93) -
        ## expand=False and no end date given is no-no
        with pytest.raises(error.DAVError):
            c.date_search(datetime(2006, 7, 13, 17, 00, 00), expand=True)

        # .. and search for it.
        r1 = c.date_search(
            datetime(2006, 7, 13, 17, 00, 00),
            datetime(2006, 7, 15, 17, 00, 00),
            expand=False,
        )
        r2 = c.search(
            event=True,
            start=datetime(2006, 7, 13, 17, 00, 00),
            end=datetime(2006, 7, 15, 17, 00, 00),
            expand=False,
        )

        assert e.instance.vevent.uid == r1[0].instance.vevent.uid
        assert e.instance.vevent.uid == r2[0].instance.vevent.uid
        assert len(r1) == 1
        assert len(r2) == 1

        ## The rest of the test code here depends on us changing an event.
        ## Apparently, in google calendar, events are immutable.
        ## TODO: delete the old event and insert a new one rather than skipping.
        ## (But events should not be immutable!  One should be able to change an event, push the changes
        ## out to all participants and all copies of the calendar, and let everyone know that it's a
        ## changed event and not a cancellation and a new event).
        self.skip_on_compatibility_flag("no_overwrite")

        # ev2 is same UID, but one year ahead.
        # The timestamp should change.
        e.data = ev2
        e.save()
        r1 = c.date_search(
            datetime(2006, 7, 13, 17, 00, 00),
            datetime(2006, 7, 15, 17, 00, 00),
            expand=False,
        )
        r2 = c.search(
            event=True,
            start=datetime(2006, 7, 13, 17, 00, 00),
            end=datetime(2006, 7, 15, 17, 00, 00),
            expand=False,
        )
        assert len(r1) == 0
        assert len(r2) == 0
        r1 = c.date_search(
            datetime(2007, 7, 13, 17, 00, 00),
            datetime(2007, 7, 15, 17, 00, 00),
            expand=False,
        )
        assert len(r1) == 1

        # date search without closing date should also find it
        r = c.date_search(datetime(2007, 7, 13, 17, 00, 00), expand=False)
        assert len(r) == 1

        # Lets try a freebusy request as well
        self.skip_on_compatibility_flag("no_freebusy_rfc4791")

        freebusy = c.freebusy_request(
            datetime(2007, 7, 13, 17, 00, 00), datetime(2007, 7, 15, 17, 00, 00)
        )
        # TODO: assert something more complex on the return object
        assert isinstance(freebusy, FreeBusy)
        assert freebusy.instance.vfreebusy

    def testRecurringDateSearch(self):
        """
        This is more sanity testing of the server side than testing of the
        library per se.  How will it behave if we serve it a recurring
        event?
        """
        self.skip_on_compatibility_flag("read_only")
        self.skip_on_compatibility_flag("no_recurring")
        self.skip_on_compatibility_flag("no_search")
        c = self._fixCalendar()

        # evr is a yearly event starting at 1997-11-02
        e = c.save_event(evr)

        ## Without "expand", we should still find it when searching over 2008 ...
        r = c.date_search(
            datetime(2008, 11, 1, 17, 00, 00),
            datetime(2008, 11, 3, 17, 00, 00),
            expand=False,
        )
        r2 = c.search(
            event=True,
            start=datetime(2008, 11, 1, 17, 00, 00),
            end=datetime(2008, 11, 3, 17, 00, 00),
            expand=False,
        )
        assert len(r) == 1
        assert len(r2) == 1

        ## With expand=True, we should find one occurrence
        ## legacy method name
        r1 = c.date_search(
            datetime(2008, 11, 1, 17, 00, 00),
            datetime(2008, 11, 3, 17, 00, 00),
            expand=True,
        )
        ## server expansion, with client side fallback
        r2 = c.search(
            event=True,
            start=datetime(2008, 11, 1, 17, 00, 00),
            end=datetime(2008, 11, 3, 17, 00, 00),
            expand=True,
        )
        ## client side expansion
        r3 = c.search(
            event=True,
            start=datetime(2008, 11, 1, 17, 00, 00),
            end=datetime(2008, 11, 3, 17, 00, 00),
            expand="client",
        )
        ## server side expansion
        r4 = c.search(
            event=True,
            start=datetime(2008, 11, 1, 17, 00, 00),
            end=datetime(2008, 11, 3, 17, 00, 00),
            expand="server",
        )
        assert len(r1) == 1
        assert len(r2) == 1
        assert r1[0].data.count("END:VEVENT") == 1
        assert r2[0].data.count("END:VEVENT") == 1
        ## due to expandation, the DTSTART should be in 2008
        if not self.check_compatibility_flag("broken_expand"):
            assert r1[0].data.count("DTSTART;VALUE=DATE:2008") == 1
            assert r2[0].data.count("DTSTART;VALUE=DATE:2008") == 1
            if not self.check_compatibility_flag("no_expand"):
                assert r4[0].data.count("DTSTART;VALUE=DATE:2008") == 1
        assert r3[0].data.count("DTSTART;VALUE=DATE:2008") == 1

        ## With expand=True and searching over two recurrences ...
        r1 = c.date_search(
            datetime(2008, 11, 1, 17, 00, 00),
            datetime(2009, 11, 3, 17, 00, 00),
            expand=True,
        )
        r2 = c.search(
            event=True,
            start=datetime(2008, 11, 1, 17, 00, 00),
            end=datetime(2009, 11, 3, 17, 00, 00),
            expand=True,
        )

        ## According to https://tools.ietf.org/html/rfc4791#section-7.8.3, the
        ## resultset should be one vcalendar with two events.
        assert len(r1) == 1
        assert "RRULE" not in r1[0].data
        if self.check_compatibility_flag("robur_rrule_freq_yearly_expands_monthly"):
            assert r1[0].data.count("END:VEVENT") == 13
        else:
            assert r1[0].data.count("END:VEVENT") == 2
        ## However, the new search method will by default split it into
        ## two events.  Or 13, in the case of robur
        if self.check_compatibility_flag("robur_rrule_freq_yearly_expands_monthly"):
            assert len(r2) == 13
        else:
            assert len(r2) == 2
        assert "RRULE" not in r2[0].data
        assert "RRULE" not in r2[1].data
        assert r2[0].data.count("END:VEVENT") == 1
        assert r2[1].data.count("END:VEVENT") == 1

        # The recurring events should not be expanded when using the
        # events() method
        r = c.events()
        if not self.check_compatibility_flag("no_mkcalendar"):
            assert len(r) == 1
        assert r[0].data.count("END:VEVENT") == 1

    def testRecurringDateWithExceptionSearch(self):
        self.skip_on_compatibility_flag("no_search")
        c = self._fixCalendar()

        # evr2 is a bi-weekly event starting 2024-04-11
        ## It has an exception, edited summary for recurrence id 20240425T123000Z
        e = c.save_event(evr2)

        r = c.search(
            start=datetime(2024, 3, 31, 0, 0),
            end=datetime(2024, 5, 4, 0, 0, 0),
            event=True,
            expand=True,
        )
        rc = c.search(
            start=datetime(2024, 3, 31, 0, 0),
            end=datetime(2024, 5, 4, 0, 0, 0),
            event=True,
            expand="client",
        )
        rs = c.search(
            start=datetime(2024, 3, 31, 0, 0),
            end=datetime(2024, 5, 4, 0, 0, 0),
            event=True,
            expand="server",
        )

        assert len(rc) == 2
        if not self.check_compatibility_flag("broken_expand"):
            assert len(r) == 2
            if not self.check_compatibility_flag("no_expand"):
                assert len(rs) == 2

        assert "RRULE" not in r[0].data
        assert "RRULE" not in r[1].data

        asserts_on_results = [rc]
        if not self.check_compatibility_flag(
            "broken_expand_on_exceptions"
        ) and not self.check_compatibility_flag("broken_expand"):
            asserts_on_results.append(r)
            if not self.check_compatibility_flag("no_expand"):
                asserts_on_results.append(rs)

        for r in asserts_on_results:
            assert isinstance(
                r[0].icalendar_component["RECURRENCE-ID"], icalendar.vDDDTypes
            )

            ## TODO: xandikos returns a datetime without a tzinfo, radicale returns a datetime with tzinfo=UTC, but perhaps other calendar servers returns the timestamp converted to localtime?

            assert r[0].icalendar_component["RECURRENCE-ID"].dt.replace(
                tzinfo=None
            ) == datetime(2024, 4, 11, 12, 30, 00)

            assert isinstance(
                r[1].icalendar_component["RECURRENCE-ID"], icalendar.vDDDTypes
            )
            assert r[1].icalendar_component["RECURRENCE-ID"].dt.replace(
                tzinfo=None
            ) == datetime(2024, 4, 25, 12, 30, 00)

    def testEditSingleRecurrence(self):
        """
        It should be possible to fetch a single recurrence from
        the calendar using search and expand, edit it and save it.
        Only the recurrence should be edited, not the rest of the
        event.
        """
        self.skip_on_compatibility_flag("no_recurring")
        cal = self._fixCalendar()

        ## Create a daily recurring event
        cal.save_event(
            uid="test1",
            summary="daily test",
            dtstart=datetime(2015, 1, 1, 8, 7, 6),
            dtend=datetime(2015, 1, 1, 9, 7, 6),
            rrule={"FREQ": "DAILY"},
        )

        def search(month):
            """
            Internal function to find one recurrence object
            """
            recurrence = cal.search(
                event=True,
                start=datetime(2015, month, 1),
                end=datetime(2015, month, 2),
                expand="client",  ## client will be default from 2.0
            )
            assert len(recurrence) == 1
            return recurrence[0]

        def summary_by_month(month):
            return search(month).icalendar_component["summary"]

        ## Search for a recurrence
        recurrence = search(7)

        ## Modify it and save it
        recurrence.icalendar_component["summary"] = "half a year of daily testing"
        recurrence.save()

        ## Only one day should be affected
        assert summary_by_month(6) == "daily test"
        assert summary_by_month(7) == "half a year of daily testing"
        assert summary_by_month(8) == "daily test"

        ## let's try to set several recurrence exceptions
        recurrence = search(2)
        recurrence.icalendar_component["summary"] = "one month of daily testing"
        recurrence.save()

        assert summary_by_month(1) == "daily test"
        assert summary_by_month(2) == "one month of daily testing"
        assert summary_by_month(7) == "half a year of daily testing"

        ## Changing any of the exceptions should also work
        recurrence = search(7)
        recurrence.icalendar_component["summary"] = "six months of daily testing"
        recurrence.save()
        assert summary_by_month(7) == "six months of daily testing"

        ## this new feature does not workk on python 3.8.  We will soon enough
        ## release 2.0 and shed the 3.8-dependency.  As for now, just skip the rest of the test.
        if sys.version_info < (3, 9):
            return

        ## parameter all_recurrences should change all recurrences -
        ## except February and July
        recurrence = search(9)
        recurrence.icalendar_component["summary"] = "daily testing"
        recurrence.save(all_recurrences=True)
        assert summary_by_month(1) == "daily testing"
        assert summary_by_month(2) == "one month of daily testing"
        assert summary_by_month(3) == "daily testing"
        assert summary_by_month(7) == "six months of daily testing"

        ## Last ... let's change the dtend and dtstart of the recurrence
        recurrence = search(9)
        recurrence.icalendar_component.pop("dtstart")
        recurrence.icalendar_component.add("dtstart", datetime(2015, 9, 1, 8, 0, 0))
        recurrence.icalendar_component.pop("dtend")
        recurrence.icalendar_component.add("dtend", datetime(2015, 9, 1, 10, 0, 0))
        recurrence.save(all_recurrences=True)

        recurrence = search(8)
        assert (
            recurrence.icalendar_component.start.astimezone()
            == datetime(2015, 8, 1, 8, 0, 0).astimezone()
        )
        assert (
            recurrence.icalendar_component.end.astimezone()
            == datetime(2015, 8, 1, 10, 0, 0).astimezone()
        )

    def testOffsetURL(self):
        """
        pass a URL pointing to a calendar or a user to the DAVClient class,
        and things should still work
        """
        urls = [self.principal.url, self._fixCalendar().url]
        connect_params = self.server_params.copy()
        for delme in ("url", "setup", "teardown", "name"):
            if delme in connect_params:
                connect_params.pop(delme)
        for url in urls:
            conn = client(**connect_params, url=url)
            principal = conn.principal()
            calendars = principal.calendars()

    def testObjects(self):
        # TODO: description ... what are we trying to test for here?
        o = DAVObject(self.caldav)
        with pytest.raises(Exception):
            o.save()


# We want to run all tests in the above class through all caldav_servers;
# and I don't really want to create a custom nose test loader.  The
# solution here seems to be to generate one child class for each
# caldav_url, and inject it into the module namespace. TODO: This is
# very hacky.  If there are better ways to do it, please let me know.
# (maybe a custom nose test loader really would be the better option?)
# -- Tobias Brox <t-caldav@tobixen.no>, 2013-10-10

## TODO: The better way is probably to use @pytest.mark.parametrize
## -- Tobias Brox <t-caldav@tobixen.no>, 2024-11-15

_servernames = set()
for _caldav_server in caldav_servers:
    if "name" in _caldav_server:
        _servername = _caldav_server["name"]
    else:
        # create a unique identifier out of the server domain name
        _parsed_url = urlparse(_caldav_server["url"])
        _servername = _parsed_url.hostname.replace(".", "_").replace("-", "_") + str(
            _parsed_url.port or ""
        )
        while _servername in _servernames:
            _servername = _servername + "_"
        _servername = _servername.capitalize()
        _servernames.add(_servername)

    # create a classname and a class
    _classname = "TestForServer" + _servername

    # inject the new class into this namespace
    vars()[_classname] = type(
        _classname,
        (RepeatedFunctionalTestsBaseClass,),
        {"server_params": _caldav_server},
    )

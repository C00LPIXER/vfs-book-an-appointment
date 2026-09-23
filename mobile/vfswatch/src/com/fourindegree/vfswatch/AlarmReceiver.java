package com.fourindegree.vfswatch;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** Fires a check and books the next one. */
public class AlarmReceiver extends BroadcastReceiver {

    @Override public void onReceive(Context c, Intent i) {
        Intent svc = new Intent(c, PollService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(svc); else c.startService(svc);
        // a safety net in case the service cannot reschedule (no network, killed mid-fetch)
        schedule(c, Prefs.intervalMin(c));
    }

    /**
     * Book the next check `minutes` from now. VFS refreshes its earliest-date data about once an
     * hour, so PollService passes "minutes until the next VFS refresh + a small margin" — that way
     * every check lands on fresh data instead of drifting against it.
     */
    static void schedule(Context c, int minutes) {
        if (minutes < 2) minutes = 2;
        AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
        PendingIntent pi = pending(c);
        long when = System.currentTimeMillis() + minutes * 60_000L;
        try {
            if (Build.VERSION.SDK_INT >= 23) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, when, pi);
            else am.setExact(AlarmManager.RTC_WAKEUP, when, pi);
        } catch (SecurityException e) {        // exact alarms not allowed: inexact still gets there
            am.set(AlarmManager.RTC_WAKEUP, when, pi);
        }
        Prefs.put(c, Prefs.NEXT_AT, when);
    }

    private static PendingIntent pending(Context c) {
        Intent i = new Intent(c, AlarmReceiver.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        return PendingIntent.getBroadcast(c, 1, i, flags);
    }
}

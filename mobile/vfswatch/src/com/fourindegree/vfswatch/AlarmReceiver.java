package com.fourindegree.vfswatch;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.SystemClock;

/** Fires the periodic check and immediately schedules the next one. */
public class AlarmReceiver extends BroadcastReceiver {

    @Override public void onReceive(Context c, Intent i) {
        Intent svc = new Intent(c, PollService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(svc); else c.startService(svc);
        schedule(c);          // chain the next one (exact alarms do not repeat by themselves)
    }

    /** Ask Android to wake us in `interval` minutes, even in doze. */
    static void schedule(Context c) {
        AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
        PendingIntent pi = pending(c);
        long when = SystemClock.elapsedRealtime() + Prefs.intervalMin(c) * 60_000L;
        try {
            if (Build.VERSION.SDK_INT >= 23) am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, when, pi);
            else am.setExact(AlarmManager.ELAPSED_REALTIME_WAKEUP, when, pi);
        } catch (SecurityException e) {      // no exact-alarm permission: an inexact one still works
            am.set(AlarmManager.ELAPSED_REALTIME_WAKEUP, when, pi);
        }
    }

    static void cancel(Context c) {
        ((AlarmManager) c.getSystemService(Context.ALARM_SERVICE)).cancel(pending(c));
    }

    private static PendingIntent pending(Context c) {
        Intent i = new Intent(c, AlarmReceiver.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        return PendingIntent.getBroadcast(c, 1, i, flags);
    }
}

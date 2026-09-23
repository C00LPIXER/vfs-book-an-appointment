package com.fourindegree.vfswatch;

import android.app.Notification;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * One check: read the public endpoint, store the result, and raise the alarm if a date appeared for
 * the watched category. Runs in the foreground so Android does not stop it halfway.
 */
public class PollService extends Service {

    static final String ACTION_DONE = "com.fourindegree.vfswatch.CHECK_DONE";

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        Alerter.ensureChannels(this);
        startForeground(1, status("Checking VFS…"));

        final PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        final PowerManager.WakeLock lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "vfswatch:poll");
        lock.acquire(90_000);

        new Fetcher(getApplicationContext()).fetch(new Fetcher.Result() {
            @Override public void ok(String json) {
                Prefs.put(PollService.this, Prefs.LAST_JSON, json);
                Prefs.put(PollService.this, Prefs.LAST_AT, System.currentTimeMillis());
                Prefs.put(PollService.this, Prefs.LAST_ERROR, "");
                handle(Json.parse(json, Prefs.category(PollService.this)));
                finish(lock);
            }
            @Override public void failed(String reason) {
                Prefs.put(PollService.this, Prefs.LAST_ERROR, reason);
                Prefs.put(PollService.this, Prefs.LAST_AT, System.currentTimeMillis());
                finish(lock);
            }
        }, 75_000);

        return START_NOT_STICKY;
    }

    /** Alarm only on a *new* opening: the same centres+dates do not ring again and again. */
    private void handle(Json.Snapshot snap) {
        if (snap.open.isEmpty()) {
            Prefs.put(this, Prefs.ALERTED, "");
            return;
        }
        StringBuilder key = new StringBuilder();
        StringBuilder text = new StringBuilder();
        for (Json.Row r : snap.open) {
            key.append(r.centre).append('=').append(r.date).append(';');
            text.append(r.centre).append(" — ").append(r.date).append(" (").append(r.category).append(")\n");
        }
        String k = key.toString();
        if (k.equals(Prefs.of(this).getString(Prefs.ALERTED, ""))) return;   // already shouted about this
        Prefs.put(this, Prefs.ALERTED, k);

        String title = snap.open.size() == 1
                ? ("D VISA OPEN — " + snap.open.get(0).centre)
                : ("D VISA OPEN — " + snap.open.size() + " centres");
        Alerter.slotFound(this, title, text.toString().trim());
        Alerter.startRinging(this);
        Alerter.call(this, Prefs.phone(this));
    }

    private void finish(PowerManager.WakeLock lock) {
        sendBroadcast(new Intent(ACTION_DONE).setPackage(getPackageName()));
        try { if (lock.isHeld()) lock.release(); } catch (Throwable ignored) { }
        stopForeground(true);
        stopSelf();
    }

    private Notification status(String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, Alerter.CH_STATUS) : new Notification.Builder(this);
        return b.setContentTitle("VFS D-Visa Watch")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .build();
    }
}

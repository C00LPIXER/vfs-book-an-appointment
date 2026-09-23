package com.fourindegree.vfswatch;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioManager;
import android.media.Ringtone;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;

/** What happens when a D-visa date appears: alarm sound, full-screen notification, phone call. */
class Alerter {

    static final String CH_ALERT = "slot_alert", CH_STATUS = "status";
    private static Ringtone ringtone;

    static void ensureChannels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);

        NotificationChannel alert = new NotificationChannel(CH_ALERT, "Slot open",
                NotificationManager.IMPORTANCE_HIGH);
        alert.setDescription("A D-visa date appeared on the VFS site");
        alert.enableVibration(true);
        alert.setVibrationPattern(new long[]{0, 600, 300, 600, 300, 600});
        alert.setBypassDnd(true);
        alert.setSound(alarmSound(), new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        nm.createNotificationChannel(alert);

        NotificationChannel status = new NotificationChannel(CH_STATUS, "Checking",
                NotificationManager.IMPORTANCE_MIN);
        status.setDescription("Quiet notice shown while the hourly check runs");
        nm.createNotificationChannel(status);
    }

    private static Uri alarmSound() {
        Uri u = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
        return u != null ? u : RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);
    }

    /** The loud part: keeps ringing on the alarm stream until the app is opened or it is stopped. */
    static void startRinging(Context c) {
        stopRinging();
        try {
            ringtone = RingtoneManager.getRingtone(c, alarmSound());
            if (ringtone != null) {
                if (Build.VERSION.SDK_INT >= 28) ringtone.setLooping(true);
                ringtone.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
                AudioManager am = (AudioManager) c.getSystemService(Context.AUDIO_SERVICE);
                am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0);
                ringtone.play();
            }
            Vibrator v = (Vibrator) c.getSystemService(Context.VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                long[] pattern = {0, 800, 400, 800, 400, 800};
                if (Build.VERSION.SDK_INT >= 26) v.vibrate(VibrationEffect.createWaveform(pattern, 0));
                else v.vibrate(pattern, 0);
            }
        } catch (Throwable ignored) { }
    }

    static void stopRinging() {
        try { if (ringtone != null && ringtone.isPlaying()) ringtone.stop(); } catch (Throwable ignored) { }
        ringtone = null;
    }

    /** Heads-up + full-screen notification carrying the centres and dates. */
    static void slotFound(Context c, String title, String detail) {
        ensureChannels(c);
        Intent open = new Intent(c, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent pi = PendingIntent.getActivity(c, 2, open, flags);

        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(c, CH_ALERT) : new Notification.Builder(c);
        b.setContentTitle(title)
         .setContentText(detail)
         .setStyle(new Notification.BigTextStyle().bigText(detail))
         .setSmallIcon(android.R.drawable.stat_notify_error)
         .setAutoCancel(true)
         .setContentIntent(pi)
         .setFullScreenIntent(pi, true)          // wakes the screen even when locked
         .setCategory(Notification.CATEGORY_ALARM)
         .setPriority(Notification.PRIORITY_MAX);
        ((NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE)).notify(100, b.build());
    }

    /** Places the call. Needs CALL_PHONE granted; without it we just leave the notification. */
    static boolean call(Context c, String number) {
        if (number == null || number.trim().isEmpty()) return false;
        try {
            Intent i = new Intent(Intent.ACTION_CALL, Uri.parse("tel:" + number.trim()))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            c.startActivity(i);
            return true;
        } catch (Throwable t) {
            return false;
        }
    }
}

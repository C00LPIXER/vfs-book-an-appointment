package com.fourindegree.vfslite;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** A plain notification when a date appears — no alarm, no call, no SMS. */
class Alerter {

    static final String CH_ALERT = "slot_open", CH_STATUS = "status";

    static void ensureChannels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);

        NotificationChannel alert = new NotificationChannel(CH_ALERT, "Slot open",
                NotificationManager.IMPORTANCE_HIGH);      // pops up with the default sound
        alert.setDescription("A date appeared on the VFS site");
        alert.enableVibration(true);
        nm.createNotificationChannel(alert);

        NotificationChannel status = new NotificationChannel(CH_STATUS, "Checking",
                NotificationManager.IMPORTANCE_MIN);       // silent, for the brief check itself
        nm.createNotificationChannel(status);
    }

    static void slotFound(Context c, String title, String detail) {
        ensureChannels(c);
        Intent open = new Intent(c, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent pi = PendingIntent.getActivity(c, 2, open, flags);

        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(c, CH_ALERT) : new Notification.Builder(c);
        ((NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE)).notify(100,
                b.setContentTitle(title)
                 .setContentText(detail)
                 .setStyle(new Notification.BigTextStyle().bigText(detail))
                 .setSmallIcon(android.R.drawable.stat_notify_more)
                 .setAutoCancel(true)
                 .setContentIntent(pi)
                 .build());
    }
}

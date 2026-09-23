package com.fourindegree.vfswatch;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Re-arm the hourly check after a reboot, so the watch survives the phone restarting. */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context c, Intent i) { AlarmReceiver.schedule(c); }
}

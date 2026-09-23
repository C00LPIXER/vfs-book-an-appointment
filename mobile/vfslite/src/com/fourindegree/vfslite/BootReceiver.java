package com.fourindegree.vfslite;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Re-arm the check after a reboot so the watch survives a restart. */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context c, Intent i) { AlarmReceiver.schedule(c, 2); }
}

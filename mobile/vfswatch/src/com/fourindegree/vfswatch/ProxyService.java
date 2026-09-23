package com.fourindegree.vfswatch;

import android.app.Notification;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * Keeps the SOCKS5 server alive so the PC can use this phone's mobile connection.
 *
 * Runs in the foreground (Android would otherwise stop it within a minute) and, from Android 6 on,
 * pins the phone's process to the *mobile* network. Without that pin, traffic would leave over WiFi
 * whenever WiFi is connected — which on the office WiFi means the very IP we are trying to avoid.
 */
public class ProxyService extends Service {

    static final String ACTION_STOP = "com.fourindegree.vfswatch.STOP_PROXY";
    static volatile boolean running;
    static volatile String boundTo = "";

    private final SocksServer socks = new SocksServer();
    private PowerManager.WakeLock lock;
    private ConnectivityManager.NetworkCallback cb;

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }

        Alerter.ensureChannels(this);
        startForeground(2, note("starting…"));

        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "vfswatch:proxy");
        lock.acquire();

        bindToMobile();
        socks.start();
        running = true;
        startForeground(2, note("PC can use this phone's data"));
        return START_STICKY;
    }

    /** Ask Android to send this app's sockets over mobile data, not WiFi. */
    private void bindToMobile() {
        final ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        NetworkRequest req = new NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build();
        cb = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network network) {
                boolean ok = Build.VERSION.SDK_INT >= 23
                        ? cm.bindProcessToNetwork(network)
                        : ConnectivityManager.setProcessDefaultNetwork(network);
                boundTo = ok ? "mobile data" : "default network";
                startForeground(2, note("PC can use this phone's " + boundTo));
            }
            @Override public void onLost(Network network) { boundTo = "waiting for mobile data"; }
        };
        try { cm.requestNetwork(req, cb); } catch (Throwable t) { boundTo = "default network"; }
    }

    @Override public void onDestroy() {
        running = false;
        socks.stop();
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cb != null) cm.unregisterNetworkCallback(cb);
            if (Build.VERSION.SDK_INT >= 23) cm.bindProcessToNetwork(null);
        } catch (Throwable ignored) { }
        try { if (lock != null && lock.isHeld()) lock.release(); } catch (Throwable ignored) { }
        super.onDestroy();
    }

    private Notification note(String text) {
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        int f = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, Alerter.CH_STATUS) : new Notification.Builder(this);
        return b.setContentTitle("Sharing this phone's connection")
                .setContentText(text + "  ·  port " + SocksServer.PORT)
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentIntent(PendingIntent.getActivity(this, 3, open, f))
                .setOngoing(true)
                .build();
    }
}

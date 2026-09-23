package com.fourindegree.vfswatch;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.format.DateUtils;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

/** One screen: when VFS last updated, what is open, and the number to ring. */
public class MainActivity extends Activity {

    private TextView updated, detail, rows;
    private EditText phone;
    private final Handler ui = new Handler(Looper.getMainLooper());

    private final BroadcastReceiver onChecked = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) { render(); }
    };

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.main);
        updated = findViewById(R.id.updated);
        detail  = findViewById(R.id.detail);
        rows    = findViewById(R.id.rows);
        phone   = findViewById(R.id.phone);

        Alerter.ensureChannels(this);
        Alerter.stopRinging();                 // opening the app silences the alarm
        askPermissions();

        phone.setText(Prefs.phone(this));
        ((Button) findViewById(R.id.save)).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                Prefs.put(MainActivity.this, Prefs.PHONE, phone.getText().toString().trim());
                Toast.makeText(MainActivity.this, "Saved", Toast.LENGTH_SHORT).show();
            }
        });
        ((Button) findViewById(R.id.refresh)).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { check(); }
        });
        ((Button) findViewById(R.id.test)).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                Alerter.slotFound(MainActivity.this, "TEST — D VISA OPEN", "Chennai — 28 Sep 2026 (Long Stay D visa)");
                Alerter.startRinging(MainActivity.this);
                ui.postDelayed(new Runnable() { public void run() { Alerter.stopRinging(); } }, 6000);
                Alerter.call(MainActivity.this, Prefs.phone(MainActivity.this));
            }
        });

        AlarmReceiver.schedule(this);           // keep the hourly check armed
        render();
        if (Prefs.of(this).getLong(Prefs.LAST_AT, 0) == 0) check();
    }

    @Override protected void onResume() {
        super.onResume();
        registerReceiver(onChecked, new IntentFilter(PollService.ACTION_DONE),
                Build.VERSION.SDK_INT >= 33 ? Context.RECEIVER_NOT_EXPORTED : 0);
        Alerter.stopRinging();
        render();
    }

    @Override protected void onPause() {
        super.onPause();
        try { unregisterReceiver(onChecked); } catch (Throwable ignored) { }
    }

    private void check() {
        updated.setText("Checking VFS…");
        Intent svc = new Intent(this, PollService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc); else startService(svc);
    }

    /** Draw whatever the last check produced. */
    private void render() {
        String json = Prefs.of(this).getString(Prefs.LAST_JSON, "");
        long at = Prefs.of(this).getLong(Prefs.LAST_AT, 0);
        String err = Prefs.of(this).getString(Prefs.LAST_ERROR, "");

        if (json.isEmpty()) {
            updated.setText(err.isEmpty() ? "No data yet" : "Could not read VFS");
            detail.setText(err);
            rows.setText("");
            return;
        }
        Json.Snapshot s = Json.parse(json, Prefs.category(this));

        // VFS's own "last updated N minutes back", kept counting from when we read it
        String vfs = s.updatedOn;
        if (s.updatedValue >= 0) {
            long extra = at > 0 ? (System.currentTimeMillis() - at) / 60000 : 0;
            long mins = s.updatedValue * unit(s.updatedType) + extra;
            vfs = (mins < 60 ? mins + " min" : (mins / 60) + " h " + (mins % 60) + " min") + " ago";
        }
        updated.setText("VFS updated " + vfs);
        detail.setText("checked " + (at > 0 ? DateUtils.getRelativeTimeSpanString(at) : "—")
                + " · " + s.centres + " centres"
                + (err.isEmpty() ? "" : "\nlast error: " + err));

        if (s.open.isEmpty()) {
            rows.setText("No " + Prefs.category(this) + " date anywhere.\n\nThe alarm rings, notifies and calls the moment one appears.");
        } else {
            StringBuilder sb = new StringBuilder();
            for (Json.Row r : s.open) sb.append("● ").append(r.centre).append(" — ").append(r.date).append('\n');
            rows.setText(sb.toString().trim());
        }
        appendAllCentres(json);
    }

    private long unit(String type) {
        if ("H".equalsIgnoreCase(type)) return 60;
        if ("D".equalsIgnoreCase(type)) return 1440;
        if ("S".equalsIgnoreCase(type)) return 0;
        return 1;
    }

    /** Below the openings, list every centre so the screen mirrors the VFS page. */
    private void appendAllCentres(String json) {
        try {
            JSONArray vac = new JSONObject(json).optJSONArray("vacList");
            StringBuilder sb = new StringBuilder("\n\nAll centres (" + Prefs.category(this) + "):\n");
            String want = Prefs.category(this).toLowerCase();
            for (int i = 0; vac != null && i < vac.length(); i++) {
                JSONObject v = vac.getJSONObject(i);
                String centre = Json.shortCentre(v.optString("vacName", ""));
                String date = "—";
                JSONArray g = v.optJSONArray("visaGroupList");
                for (int j = 0; g != null && j < g.length(); j++) {
                    JSONObject x = g.getJSONObject(j);
                    if (x.optString("displayName", "").toLowerCase().contains(want)) {
                        String d = x.optString("earliestAvailableDate", "");
                        date = d.isEmpty() ? "no date" : d;
                    }
                }
                sb.append(String.format("%-12s %s\n", centre, date));
            }
            rows.append(sb.toString());
        } catch (Exception ignored) { }
    }

    private void askPermissions() {
        if (Build.VERSION.SDK_INT >= 23) {
            java.util.List<String> need = new java.util.ArrayList<>();
            if (Build.VERSION.SDK_INT >= 33
                    && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                need.add(Manifest.permission.POST_NOTIFICATIONS);
            if (checkSelfPermission(Manifest.permission.CALL_PHONE) != PackageManager.PERMISSION_GRANTED)
                need.add(Manifest.permission.CALL_PHONE);
            if (!need.isEmpty()) requestPermissions(need.toArray(new String[0]), 7);
        }
    }
}

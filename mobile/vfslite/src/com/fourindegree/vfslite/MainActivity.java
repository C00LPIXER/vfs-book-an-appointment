package com.fourindegree.vfslite;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * Read-only view of the VFS public data, for someone who just wants to look:
 * is anything open, when VFS last refreshed, when we look again, and every centre.
 * No settings, no phone number, no alarm — a notification is the only alert.
 */
public class MainActivity extends Activity {

    private TextView verdict, verdictSub, updated, updatedRaw, next, nextRaw, error;
    private LinearLayout list, verdictBox;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final SimpleDateFormat clock = new SimpleDateFormat("h:mm a", Locale.ENGLISH);

    private final BroadcastReceiver onChecked = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) { render(); }
    };
    private final Runnable ticker = new Runnable() {
        public void run() { render(); ui.postDelayed(this, 30_000); }
    };

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.main);
        verdict = findViewById(R.id.verdict);       verdictSub = findViewById(R.id.verdictSub);
        verdictBox = findViewById(R.id.verdictBox);
        updated = findViewById(R.id.updated);       updatedRaw = findViewById(R.id.updatedRaw);
        next = findViewById(R.id.next);             nextRaw = findViewById(R.id.nextRaw);
        error = findViewById(R.id.error);           list = findViewById(R.id.list);

        Alerter.ensureChannels(this);
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7);

        findViewById(R.id.refresh).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { check(); }
        });

        AlarmReceiver.schedule(this, Prefs.intervalMin(this));
        render();
        if (Prefs.of(this).getLong(Prefs.LAST_AT, 0) == 0) check();
    }

    @Override protected void onResume() {
        super.onResume();
        registerReceiver(onChecked, new IntentFilter(PollService.ACTION_DONE),
                Build.VERSION.SDK_INT >= 33 ? Context.RECEIVER_NOT_EXPORTED : 0);
        ui.post(ticker);
    }

    @Override protected void onPause() {
        super.onPause();
        ui.removeCallbacks(ticker);
        try { unregisterReceiver(onChecked); } catch (Throwable ignored) { }
    }

    private void check() {
        verdict.setText("checking…");
        verdictSub.setText("");
        Intent svc = new Intent(this, PollService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc); else startService(svc);
    }

    private void render() {
        String body = Prefs.of(this).getString(Prefs.LAST_JSON, "");
        long at = Prefs.of(this).getLong(Prefs.LAST_AT, 0);
        long nextAt = Prefs.of(this).getLong(Prefs.NEXT_AT, 0);
        String err = Prefs.of(this).getString(Prefs.LAST_ERROR, "");

        error.setVisibility(err.isEmpty() ? View.GONE : View.VISIBLE);
        error.setText(err);

        next.setText(nextAt > 0 ? clock.format(new Date(nextAt)) : "—");
        nextRaw.setText(nextAt > 0 ? "in " + until(nextAt) : "");

        if (body.isEmpty()) {
            verdict.setText(err.isEmpty() ? "loading…" : "cannot reach VFS");
            verdictSub.setText(err.isEmpty() ? "" : "it will try again automatically");
            updated.setText("—");
            return;
        }

        Json.Snapshot s = Json.parse(body, Prefs.category(this));
        long mins = s.updatedValue < 0 ? -1 : s.updatedValue * unit(s.updatedType)
                + (at > 0 ? (System.currentTimeMillis() - at) / 60000 : 0);
        updated.setText(mins < 0 ? "—" : (mins < 60 ? mins + " min ago" : (mins / 60) + " h " + (mins % 60) + " m ago"));
        updatedRaw.setText(at > 0 ? "we looked at " + clock.format(new Date(at)) : "");

        if (s.open.isEmpty()) {
            verdict.setText("No slots open");
            verdict.setTextColor(0xFFC9D3E0);
            verdictSub.setText("You will get a notification the moment one appears.");
        } else {
            StringBuilder sb = new StringBuilder();
            for (Json.Row r : s.open) sb.append(r.centre).append(" — ").append(r.date).append('\n');
            verdict.setText(s.open.size() == 1 ? "SLOT OPEN" : s.open.size() + " SLOTS OPEN");
            verdict.setTextColor(0xFF5BD98A);
            verdictSub.setText(sb.toString().trim());
        }
        drawList(body);
    }

    /** Every centre, with its date or a quiet dash. */
    private void drawList(String body) {
        list.removeAllViews();
        String want = Prefs.category(this).toLowerCase();
        List<String[]> rows = new ArrayList<>();
        try {
            JSONArray vac = new JSONObject(body).optJSONArray("vacList");
            for (int i = 0; vac != null && i < vac.length(); i++) {
                JSONObject v = vac.getJSONObject(i);
                String centre = Json.shortCentre(v.optString("vacName", ""));
                String date = "";
                JSONArray g = v.optJSONArray("visaGroupList");
                for (int j = 0; g != null && j < g.length(); j++) {
                    JSONObject x = g.getJSONObject(j);
                    if (x.optString("displayName", "").toLowerCase().contains(want))
                        date = x.optString("earliestAvailableDate", "");
                }
                rows.add(new String[]{centre, date});
            }
        } catch (Exception ignored) { }

        for (String[] r : rows) {
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(dp(12), dp(12), dp(12), dp(12));

            TextView name = new TextView(this);
            name.setText(r[0]);
            name.setTextColor(0xFFE6EAF2);
            name.setTextSize(15);
            name.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1f));

            boolean open = r[1] != null && !r[1].isEmpty();
            TextView pill = new TextView(this);
            pill.setText(open ? r[1] : "—");
            pill.setTextColor(open ? 0xFF5BD98A : 0xFF55606E);
            pill.setTextSize(open ? 14 : 15);
            pill.setTypeface(null, open ? Typeface.BOLD : Typeface.NORMAL);
            pill.setBackgroundResource(open ? R.drawable.pill_ok : R.drawable.pill_none);
            pill.setPadding(dp(14), dp(7), dp(14), dp(7));

            row.addView(name);
            row.addView(pill);
            list.addView(row);
        }
    }

    private String until(long when) {
        long m = (when - System.currentTimeMillis()) / 60000;
        return m <= 0 ? "moments" : m < 60 ? m + " min" : (m / 60) + " h " + (m % 60) + " m";
    }

    private long unit(String type) {
        if ("H".equalsIgnoreCase(type)) return 60;
        if ("D".equalsIgnoreCase(type)) return 1440;
        if ("S".equalsIgnoreCase(type)) return 0;
        return 1;
    }

    private int dp(int v) { return (int) (v * getResources().getDisplayMetrics().density); }
}

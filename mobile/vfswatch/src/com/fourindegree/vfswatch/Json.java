package com.fourindegree.vfswatch;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** Small helpers over the endpoint's response (org.json ships with Android). */
class Json {

    /** A slot line: centre + category + the date VFS shows (empty date = nothing open). */
    static class Row {
        final String centre, category, date;
        Row(String c, String cat, String d) { centre = c; category = cat; date = d; }
    }

    static class Snapshot {
        String updatedOn = "";     // VFS's own stamp, e.g. "2026-9-23 6:49:57"
        int updatedValue = -1;     // "last updated N ..." as VFS reports it
        String updatedType = "";   // M = minutes, H = hours, S = seconds
        final List<Row> open = new ArrayList<>();   // rows with a date for the watched category
        int centres;
    }

    /** evaluateJavascript returns a JSON *string literal*; turn it back into the text inside. */
    static String unquote(String v) {
        if (v == null) return "";
        try { return new JSONArray("[" + v + "]").getString(0); } catch (Exception e) { return v; }
    }

    static int intField(String json, String name) {
        try { return new JSONObject(json).optInt(name, 0); } catch (Exception e) { return 0; }
    }

    static String stringField(String json, String name) {
        try { return new JSONObject(json).optString(name, null); } catch (Exception e) { return null; }
    }

    /** Pull out the "last updated" stamp and every centre that has a date for `category`. */
    static Snapshot parse(String body, String category) {
        Snapshot s = new Snapshot();
        try {
            JSONObject o = new JSONObject(body);
            s.updatedOn = o.optString("lastUpdatedOn", "");
            s.updatedValue = o.optInt("lastUpdatedValue", -1);
            s.updatedType = o.optString("lastUpdatedType", "");
            JSONArray vac = o.optJSONArray("vacList");
            s.centres = vac == null ? 0 : vac.length();
            for (int i = 0; vac != null && i < vac.length(); i++) {
                JSONObject v = vac.getJSONObject(i);
                String centre = shortCentre(v.optString("vacName", ""));
                JSONArray groups = v.optJSONArray("visaGroupList");
                for (int j = 0; groups != null && j < groups.length(); j++) {
                    JSONObject g = groups.getJSONObject(j);
                    String cat = g.optString("displayName", "");
                    String date = g.optString("earliestAvailableDate", "");
                    if (date != null && date.length() > 0
                            && cat.toLowerCase().contains(category.toLowerCase())) {
                        s.open.add(new Row(centre, cat, date));
                    }
                }
            }
        } catch (Exception e) { /* a malformed body just means "nothing found" */ }
        return s;
    }

    /** "Bulgaria Visa Application Centre-Cochin" -> "Cochin" */
    static String shortCentre(String full) {
        if (full == null) return "";
        int i = Math.max(full.lastIndexOf('-'), full.lastIndexOf(','));
        String s = i >= 0 ? full.substring(i + 1) : full;
        return s.trim();
    }
}

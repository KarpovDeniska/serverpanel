// Backup plan builder — sources + destinations with add/remove rows.
// Hidden inputs `sources` and `destinations` are serialized to JSON on submit.

(function () {
    const root = document.getElementById("plan-builder");
    if (!root) return;

    const SOURCE_TYPES = ["dir", "file", "vss_dir"];
    const COMPRESS_KINDS = ["none", "zip"];
    const FREQUENCIES = ["daily", "weekly"];

    // JSON payloads live in <script type="application/json"> blocks (not in
     // data-attributes) so `"` inside the payload doesn't break attribute
     // parsing and kill the whole builder on load.
    function readJson(id, fallback) {
        const el = document.getElementById(id);
        if (!el) return fallback;
        const txt = (el.textContent || "").trim();
        if (!txt) return fallback;
        try { return JSON.parse(txt); }
        catch (e) { console.error("bad JSON in " + id, e, txt); return fallback; }
    }
    const storages = readJson("plan-storages-data", []);
    const state = {
        sources: readJson("plan-sources-data", []),
        destinations: readJson("plan-destinations-data", []),
    };

    const listsEl = {
        sources: root.querySelector('[data-list="sources"]'),
        destinations: root.querySelector('[data-list="destinations"]'),
    };
    const previewEl = root.querySelector("[data-preview]");
    const hiddenSources = root.querySelector('input[name="sources"]');
    const hiddenDestinations = root.querySelector('input[name="destinations"]');

    const NEW_SOURCE = () => ({ alias: "", type: "dir", path: "", compress: "none" });
    const NEW_LOCAL = () => ({ kind: "local", base_path: "", aliases: [], rotation_days: null, date_folder: false });
    const NEW_STORAGE = () => ({
        kind: "storage",
        storage_config_id: storages[0] ? storages[0].id : 0,
        base_path: "backups/daily",
        aliases: [],
        rotation_days: null,
        date_folder: true,
        frequency: "daily",
    });

    // -----------------------------------------------------------------------
    // Primitives
    // -----------------------------------------------------------------------

    function makeInput({ value, placeholder, type = "text" }) {
        const el = document.createElement("input");
        el.type = type;
        el.className = "w-full bg-dark-bg border border-dark-border rounded px-2 py-1 text-xs";
        if (placeholder) el.placeholder = placeholder;
        if (type === "checkbox") {
            el.checked = !!value;
            el.className = "bg-dark-bg border border-dark-border rounded";
        } else {
            el.value = value == null ? "" : value;
        }
        return el;
    }

    function makeSelect({ value, options }) {
        const el = document.createElement("select");
        el.className = "w-full bg-dark-bg border border-dark-border rounded px-2 py-1 text-xs";
        options.forEach((opt) => {
            const o = document.createElement("option");
            if (typeof opt === "object") {
                o.value = opt.value;
                o.textContent = opt.label;
            } else {
                o.value = o.textContent = opt;
            }
            if (String(opt.value ?? opt) === String(value)) o.selected = true;
            el.appendChild(o);
        });
        return el;
    }

    function field(labelText, inputEl, { span = 1 } = {}) {
        const wrap = document.createElement("div");
        wrap.className = `col-span-${span}`;
        const lbl = document.createElement("div");
        lbl.className = "text-[10px] uppercase tracking-wide text-dark-muted mb-1";
        lbl.textContent = labelText;
        wrap.appendChild(lbl);
        wrap.appendChild(inputEl);
        return wrap;
    }

    function removeBtn(onClick) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = "✕";
        b.className = "text-xs text-red-400 hover:text-red-300 px-2 py-1";
        b.addEventListener("click", onClick);
        return b;
    }

    function rowCard(badgeText, gridChildren, delBtn) {
        const card = document.createElement("div");
        card.className = "bg-dark-card border border-dark-border rounded p-3";

        const header = document.createElement("div");
        header.className = "flex items-center justify-between mb-2";
        const badge = document.createElement("span");
        badge.className = "text-xs font-semibold text-dark-accent";
        badge.textContent = badgeText;
        header.appendChild(badge);
        header.appendChild(delBtn);
        card.appendChild(header);

        const grid = document.createElement("div");
        grid.className = "grid grid-cols-12 gap-2";
        gridChildren.forEach((c) => grid.appendChild(c));
        card.appendChild(grid);
        return card;
    }

    // -----------------------------------------------------------------------
    // Row renderers
    // -----------------------------------------------------------------------

    function renderSource(src, idx) {
        const alias = makeInput({ value: src.alias, placeholder: "UNF, wwwroot…" });
        const type = makeSelect({ value: src.type, options: SOURCE_TYPES });
        const path = makeInput({ value: src.path, placeholder: "C:\\Users или /var/lib" });
        const compress = makeSelect({ value: src.compress || "none", options: COMPRESS_KINDS });

        alias.addEventListener("input", () => { src.alias = alias.value; sync(); });
        type.addEventListener("change", () => { src.type = type.value; sync(); });
        path.addEventListener("input", () => { src.path = path.value; sync(); });
        compress.addEventListener("change", () => { src.compress = compress.value; sync(); });

        const del = removeBtn(() => { state.sources.splice(idx, 1); render(); });
        return rowCard(
            `Source #${idx + 1}`,
            [
                field("alias", alias, { span: 3 }),
                field("type", type, { span: 2 }),
                field("path", path, { span: 5 }),
                field("compress", compress, { span: 2 }),
            ],
            del,
        );
    }

    function renderAliases(dest) {
        const el = makeInput({
            value: (dest.aliases || []).join(","),
            placeholder: "пусто = все источники, либо через запятую",
        });
        el.addEventListener("input", () => {
            dest.aliases = el.value.split(",").map((s) => s.trim()).filter(Boolean);
            sync();
        });
        return field("aliases", el, { span: 12 });
    }

    function renderLocal(dest, idx) {
        const base = makeInput({ value: dest.base_path, placeholder: "D:\\backups" });
        const dateFolder = makeInput({ type: "checkbox", value: dest.date_folder });
        const rot = makeInput({ type: "number", value: dest.rotation_days, placeholder: "например 14" });

        base.addEventListener("input", () => { dest.base_path = base.value; sync(); });
        dateFolder.addEventListener("change", () => { dest.date_folder = dateFolder.checked; sync(); });
        rot.addEventListener("input", () => {
            dest.rotation_days = rot.value === "" ? null : parseInt(rot.value, 10);
            sync();
        });

        const del = removeBtn(() => { state.destinations.splice(idx, 1); render(); });
        return rowCard(
            `Local destination #${idx + 1}`,
            [
                field("base_path", base, { span: 7 }),
                field("date folder", dateFolder, { span: 2 }),
                field("rotation days", rot, { span: 3 }),
                renderAliases(dest),
            ],
            del,
        );
    }

    function renderStorage(dest, idx) {
        const storageOpts = storages.map((s) => ({
            value: s.id, label: `#${s.id} ${s.name} (${s.type})`,
        }));
        const storage = makeSelect({ value: dest.storage_config_id, options: storageOpts });
        const base = makeInput({ value: dest.base_path, placeholder: "backups/daily" });
        const freq = makeSelect({ value: dest.frequency, options: FREQUENCIES });
        const dateFolder = makeInput({ type: "checkbox", value: dest.date_folder });
        const rot = makeInput({ type: "number", value: dest.rotation_days, placeholder: "например 14" });

        storage.addEventListener("change", () => {
            dest.storage_config_id = parseInt(storage.value, 10);
            sync();
        });
        base.addEventListener("input", () => { dest.base_path = base.value; sync(); });
        freq.addEventListener("change", () => { dest.frequency = freq.value; sync(); });
        dateFolder.addEventListener("change", () => { dest.date_folder = dateFolder.checked; sync(); });
        rot.addEventListener("input", () => {
            dest.rotation_days = rot.value === "" ? null : parseInt(rot.value, 10);
            sync();
        });

        const del = removeBtn(() => { state.destinations.splice(idx, 1); render(); });
        return rowCard(
            `Storage destination #${idx + 1}`,
            [
                field("storage", storage, { span: 4 }),
                field("base_path", base, { span: 4 }),
                field("frequency", freq, { span: 2 }),
                field("date folder", dateFolder, { span: 1 }),
                field("rotation", rot, { span: 1 }),
                renderAliases(dest),
            ],
            del,
        );
    }

    // -----------------------------------------------------------------------

    function render() {
        listsEl.sources.innerHTML = "";
        state.sources.forEach((s, i) => listsEl.sources.appendChild(renderSource(s, i)));

        listsEl.destinations.innerHTML = "";
        state.destinations.forEach((d, i) => {
            if (d.kind === "storage") listsEl.destinations.appendChild(renderStorage(d, i));
            else listsEl.destinations.appendChild(renderLocal(d, i));
        });

        sync();
    }

    function sync() {
        hiddenSources.value = JSON.stringify(state.sources);
        hiddenDestinations.value = JSON.stringify(state.destinations);
        if (previewEl) {
            previewEl.textContent =
                "sources: " + JSON.stringify(state.sources, null, 2) +
                "\n\ndestinations: " + JSON.stringify(state.destinations, null, 2);
        }
    }

    root.querySelectorAll("[data-add]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const kind = btn.dataset.add;
            if (kind === "source") state.sources.push(NEW_SOURCE());
            else if (kind === "local") state.destinations.push(NEW_LOCAL());
            else if (kind === "storage") {
                if (!storages.length) return;
                state.destinations.push(NEW_STORAGE());
            }
            render();
        });
    });

    render();
})();

// Backup plan builder — sources + destinations with add/remove rows.
// Hidden inputs `sources` and `destinations` are serialized to JSON on submit.

(function () {
    const root = document.getElementById("plan-builder");
    if (!root) return;

    const SOURCE_TYPES = ["dir", "file", "vss_dir"];
    const COMPRESS_KINDS = ["none", "zip"];
    const FREQUENCIES = ["daily", "weekly"];

    const storages = JSON.parse(root.dataset.storages || "[]");
    const state = {
        sources: JSON.parse(root.dataset.sources || "[]"),
        destinations: JSON.parse(root.dataset.destinations || "[]"),
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

    function inputEl(kind, name, value, opts = {}) {
        const el = document.createElement(kind);
        el.className = "bg-dark-bg border border-dark-border rounded px-2 py-1 text-xs";
        if (opts.full) el.className += " w-full";
        el.dataset.field = name;
        if (kind === "input") {
            el.type = opts.type || "text";
            if (opts.placeholder) el.placeholder = opts.placeholder;
            el.value = value == null ? "" : value;
        } else if (kind === "select") {
            (opts.options || []).forEach((opt) => {
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
        }
        return el;
    }

    function label(text) {
        const l = document.createElement("span");
        l.className = "text-xs text-dark-muted";
        l.textContent = text;
        return l;
    }

    function removeBtn(onClick) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = "✕";
        b.className = "text-xs text-red-400 hover:text-red-300 px-2";
        b.addEventListener("click", onClick);
        return b;
    }

    function rowWrap(children) {
        const row = document.createElement("div");
        row.className = "bg-dark-card border border-dark-border rounded p-2 flex flex-wrap items-center gap-2";
        children.forEach((c) => row.appendChild(c));
        return row;
    }

    function renderSource(src, idx) {
        const alias = inputEl("input", "alias", src.alias, { placeholder: "alias (UNF, wwwroot…)" });
        const type = inputEl("select", "type", src.type, { options: SOURCE_TYPES });
        const path = inputEl("input", "path", src.path, { placeholder: "C:\\Users или /var/lib", full: true });
        path.className += " flex-1 min-w-[200px]";
        const compress = inputEl("select", "compress", src.compress || "none", { options: COMPRESS_KINDS });
        const del = removeBtn(() => { state.sources.splice(idx, 1); render(); });

        [alias, type, path, compress].forEach((el) => {
            el.addEventListener("input", () => {
                src[el.dataset.field] = el.value;
                sync();
            });
            el.addEventListener("change", () => {
                src[el.dataset.field] = el.value;
                sync();
            });
        });

        return rowWrap([label("#" + (idx + 1)), alias, type, path, compress, del]);
    }

    function aliasesInput(dest) {
        const el = inputEl(
            "input", "aliases",
            (dest.aliases || []).join(","),
            { placeholder: "aliases: empty = все, иначе через запятую", full: true },
        );
        el.addEventListener("input", () => {
            dest.aliases = el.value.split(",").map((s) => s.trim()).filter(Boolean);
            sync();
        });
        return el;
    }

    function renderLocal(dest, idx) {
        const base = inputEl("input", "base_path", dest.base_path, { placeholder: "D:\\backups", full: true });
        base.className += " flex-1 min-w-[200px]";
        base.addEventListener("input", () => { dest.base_path = base.value; sync(); });

        const dateFolder = inputEl("input", "date_folder", null, { type: "checkbox" });
        dateFolder.checked = !!dest.date_folder;
        dateFolder.addEventListener("change", () => { dest.date_folder = dateFolder.checked; sync(); });

        const rot = inputEl("input", "rotation_days", dest.rotation_days, { type: "number", placeholder: "rot days (опц)" });
        rot.className += " w-24";
        rot.addEventListener("input", () => {
            dest.rotation_days = rot.value === "" ? null : parseInt(rot.value, 10);
            sync();
        });

        const del = removeBtn(() => { state.destinations.splice(idx, 1); render(); });

        const row = rowWrap([label("local #" + (idx + 1)), base, label("date_folder"), dateFolder, rot, del]);
        const row2 = document.createElement("div");
        row2.className = "mt-1";
        row2.appendChild(aliasesInput(dest));
        const wrap = document.createElement("div");
        wrap.appendChild(row);
        wrap.appendChild(row2);
        return wrap;
    }

    function renderStorage(dest, idx) {
        const storageOpts = storages.map((s) => ({
            value: s.id, label: `#${s.id} ${s.name} (${s.type})`,
        }));
        const storage = inputEl("select", "storage_config_id", dest.storage_config_id, { options: storageOpts });
        storage.addEventListener("change", () => {
            dest.storage_config_id = parseInt(storage.value, 10);
            sync();
        });

        const base = inputEl("input", "base_path", dest.base_path, { placeholder: "backups/daily" });
        base.className += " flex-1 min-w-[160px]";
        base.addEventListener("input", () => { dest.base_path = base.value; sync(); });

        const freq = inputEl("select", "frequency", dest.frequency, { options: FREQUENCIES });
        freq.addEventListener("change", () => { dest.frequency = freq.value; sync(); });

        const dateFolder = inputEl("input", "date_folder", null, { type: "checkbox" });
        dateFolder.checked = !!dest.date_folder;
        dateFolder.addEventListener("change", () => { dest.date_folder = dateFolder.checked; sync(); });

        const rot = inputEl("input", "rotation_days", dest.rotation_days, { type: "number", placeholder: "rot" });
        rot.className += " w-20";
        rot.addEventListener("input", () => {
            dest.rotation_days = rot.value === "" ? null : parseInt(rot.value, 10);
            sync();
        });

        const del = removeBtn(() => { state.destinations.splice(idx, 1); render(); });

        const row = rowWrap([
            label("storage #" + (idx + 1)), storage, base, freq,
            label("date_folder"), dateFolder, rot, del,
        ]);
        const row2 = document.createElement("div");
        row2.className = "mt-1";
        row2.appendChild(aliasesInput(dest));
        const wrap = document.createElement("div");
        wrap.appendChild(row);
        wrap.appendChild(row2);
        return wrap;
    }

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

import { createContext, useContext } from "react";

const S = {
  nl: {
    live: "Live", history: "Historie", status: "Status", settings: "Instellingen", logout: "Uitloggen",
    importing: "Afname", exporting: "Teruglevering", now: "Nu", today: "Vandaag", import: "Afname",
    export: "Teruglevering", net: "Netto", gas: "Gas", lastHour: "Afgelopen uur", phases: "Fasen",
    total: "Totaal", day: "Dag", week: "Week", month: "Maand", year: "Jaar", all: "Alles",
    prev: "Vorige", next: "Volgende", thisPeriod: "Nu", previousPeriod: "Vorige periode",
    lastYear: "Zelfde periode vorig jaar", compare: "Vergelijking", tariff: "Tarief",
    t1: "Laag (T1)", t2: "Normaal (T2)", power: "Vermogen", avg: "gemiddeld", downloadCsv: "CSV downloaden",
    resolution: "Resolutie", noData: "Nog geen gegevens voor deze periode", login: "Inloggen",
    username: "Gebruikersnaam", password: "Wachtwoord", setupTitle: "Eerste keer: maak een beheerder aan",
    setupBtn: "Beheerder aanmaken", users: "Gebruikers", role: "Rol", admin: "Beheerder", viewer: "Kijker",
    addUser: "Gebruiker toevoegen", delete: "Verwijderen", language: "Taal", changePassword: "Wachtwoord wijzigen",
    newPassword: "Nieuw wachtwoord", save: "Opslaan", saved: "Opgeslagen", account: "Account",
    collecting: "verzamelt gegevens", notCollecting: "verzamelt geen gegevens", collector: "Collector",
    meter: "Meter", database: "Database en back-up", events: "Recente gebeurtenissen", noEvents: "Nog geen gebeurtenissen",
    online: "Online", lastReading: "Laatste meting", readingsLastHour: "Metingen afgelopen uur",
    failures: "Mislukt op rij", buffered: "In buffer", runningSince: "Actief sinds", lastError: "Laatste fout",
    model: "Model", firmware: "Firmware", size: "Grootte", historySince: "Historie sinds", rawSince: "Ruwe data sinds",
    epoch: "Meter-epoch", schema: "Schema", lastBackup: "Laatste back-up", never: "nooit", yes: "ja", no: "nee",
    vsPrev: "t.o.v. vorige periode", vsLastYear: "t.o.v. vorig jaar", zoomHint: "Zoom met de schuifbalk of het scrollwiel",
    powerDetail: "Vermogen in detail", minMax: "min/max",
  },
  en: {
    live: "Live", history: "History", status: "Status", settings: "Settings", logout: "Log out",
    importing: "Importing", exporting: "Exporting", now: "Now", today: "Today", import: "Import",
    export: "Export", net: "Net", gas: "Gas", lastHour: "Last hour", phases: "Phases",
    total: "Total", day: "Day", week: "Week", month: "Month", year: "Year", all: "All",
    prev: "Previous", next: "Next", thisPeriod: "Now", previousPeriod: "Previous period",
    lastYear: "Same period last year", compare: "Comparison", tariff: "Tariff",
    t1: "Low (T1)", t2: "Normal (T2)", power: "Power", avg: "average", downloadCsv: "Download CSV",
    resolution: "Resolution", noData: "No data for this period yet", login: "Log in",
    username: "Username", password: "Password", setupTitle: "First run: create an admin",
    setupBtn: "Create admin", users: "Users", role: "Role", admin: "Admin", viewer: "Viewer",
    addUser: "Add user", delete: "Delete", language: "Language", changePassword: "Change password",
    newPassword: "New password", save: "Save", saved: "Saved", account: "Account",
    collecting: "collecting", notCollecting: "not collecting", collector: "Collector",
    meter: "Meter", database: "Database & backup", events: "Recent events", noEvents: "No events yet",
    online: "Online", lastReading: "Last reading", readingsLastHour: "Readings last hour",
    failures: "Failures in a row", buffered: "Buffered", runningSince: "Running since", lastError: "Last error",
    model: "Model", firmware: "Firmware", size: "Size", historySince: "History since", rawSince: "Raw data since",
    epoch: "Meter epoch", schema: "Schema", lastBackup: "Last backup", never: "never", yes: "yes", no: "no",
    vsPrev: "vs previous period", vsLastYear: "vs last year", zoomHint: "Zoom with the slider or scroll wheel",
    powerDetail: "Power in detail", minMax: "min/max",
  },
};

export const LangContext = createContext("nl");
export const useT = () => {
  const lang = useContext(LangContext);
  return (k) => S[lang]?.[k] ?? S.en[k] ?? k;
};
export const locale = (lang) => (lang === "en" ? "en-GB" : "nl-NL");
export const useLocale = () => locale(useContext(LangContext));

export const fmt = (loc, v, d = 0) =>
  v == null || Number.isNaN(v) ? "–" : Number(v).toLocaleString(loc, { minimumFractionDigits: d, maximumFractionDigits: d });

; ═══════════════════════════════════════════════════════════════════════════
;  Installeur de l'Agent d'impression d'étiquettes
;  Compile avec Inno Setup Compiler (https://jrsoftware.org/isdl.php)
; ═══════════════════════════════════════════════════════════════════════════

#define AppName "PrintAgent"
#define AppVersion "2.0.0"
#define InstallDir "C:\Program Files\print_agent"
#define ConfigDir "C:\ProgramData\PrintAgent"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
; Icône du fichier setup.exe lui-même
SetupIconFile=logo.ico
; Icône affichée dans "Programmes et fonctionnalités" (désinstallation)
UninstallDisplayIcon={#InstallDir}\dist\printagent.exe
; Dossier d'installation par défaut (les fichiers .exe iront dans dist\)
DefaultDirName={#InstallDir}
; Pas de choix de dossier programme dans le menu démarrer
DisableProgramGroupPage=yes
; L'installation dans Program Files nécessite les droits admin
PrivilegesRequired=admin
; Nom du fichier setup.exe généré
OutputBaseFilename=PrintAgent-Setup
; Compression
Compression=lzma2
SolidCompression=yes
; Architecture 64 bits
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
; Le binaire va dans le sous-dossier dist\ de l'installation.
; Plus de nssm.exe : l'agent tourne desormais via une tache planifiee
; "a la connexion" de l'utilisateur Windows, pas un service LocalSystem
; (les preferences d'impression/format de papier sont enregistrees PAR
; COMPTE Windows ; un service LocalSystem herite d'un format par defaut
; sans rapport avec celui configure manuellement sur le compte de l'utilisateur).
Source: "printagent.exe"; DestDir: "{#InstallDir}\dist"; Flags: ignoreversion

[Code]
{ ─────────────────────────────────────────────────────────────────────────
  Pascal Script : logique custom de l'installeur.
  On crée une page de saisie personnalisée (URL serveur + token + compte
  Windows), on génère agent.ini, puis on crée une tâche planifiée qui lance
  l'agent à la connexion de ce compte (au lieu d'un service Windows).
  ───────────────────────────────────────────────────────────────────────── }

var
  ConfigPage: TInputQueryWizardPage;

{ Crée la page de saisie custom après la page de sélection du dossier }
procedure InitializeWizard();
begin
  ConfigPage := CreateInputQueryPage(
    wpSelectDir,
    'Configuration de l''agent',
    'Paramètres de connexion au serveur et compte d''exécution',
    'Renseignez l''URL du serveur, le token de cet agent ' +
    '(copié depuis l''administration Django) et le compte Windows ' +
    'sous lequel l''agent doit s''exécuter (celui qui reste connecté ' +
    'sur ce poste et dont le pilote d''imprimante est configuré).'
  );
  { Champ 0 : URL serveur, avec valeur par défaut }
  ConfigPage.Add('URL du serveur (ex: https://mon-serveur.fr) :', False);
  ConfigPage.Values[0] := 'http://localhost:8000';
  { Champ 1 : token. }
  ConfigPage.Add('Token de l''agent :', False);
  { Champ 2 : compte Windows (DOMAINE\utilisateur), prérempli avec le
    compte qui exécute l'installeur (souvent le bon, à corriger si besoin) }
  ConfigPage.Add('Compte Windows (DOMAINE\utilisateur) :', False);
  ConfigPage.Values[2] := GetEnv('USERDOMAIN') + '\' + GetEnv('USERNAME');
end;

{ Validation : on empêche d'avancer si un champ est vide }
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ConfigPage.ID then
  begin
    if Trim(ConfigPage.Values[0]) = '' then
    begin
      MsgBox('L''URL du serveur est obligatoire.', mbError, MB_OK);
      Result := False;
    end
    else if Trim(ConfigPage.Values[1]) = '' then
    begin
      MsgBox('Le token est obligatoire.', mbError, MB_OK);
      Result := False;
    end
    else if Trim(ConfigPage.Values[2]) = '' then
    begin
      MsgBox('Le compte Windows est obligatoire.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ Écrit le fichier agent.ini dans C:\ProgramData\PrintAgent\ }
procedure WriteConfigFile();
var
  ConfigContent: String;
begin
  { Crée le dossier de config s'il n'existe pas }
  if not DirExists('{#ConfigDir}') then
    CreateDir('{#ConfigDir}');

  ConfigContent :=
    '[agent]' + #13#10 +
    'server = ' + Trim(ConfigPage.Values[0]) + #13#10 +
    'token = ' + Trim(ConfigPage.Values[1]) + #13#10;

  SaveStringToFile('{#ConfigDir}\agent.ini', ConfigContent, False);
end;

{ Supprime un éventuel ancien service NSSM "PrintAgent" (installations
  précédentes de cet installeur, avant le passage en tâche planifiée) }
procedure RemoveLegacyService();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#AppName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
  Exec(ExpandConstant('{sys}\sc.exe'), 'delete {#AppName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ Crée la tâche planifiée "à la connexion" pour le compte Windows saisi }
procedure CreateLogonTask();
var
  ResultCode: Integer;
  TaskRun: String;
  Params: String;
begin
  { Supprime une tâche existante du même nom avant de la recréer (idempotent) }
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "{#AppName}" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  TaskRun := '\"' + ExpandConstant('{#InstallDir}\dist\printagent.exe') + '\" --config \"' +
    ExpandConstant('{#ConfigDir}\agent.ini') + '\"';

  { /SC ONLOGON + /IT (interactive token) : la tâche tourne dans la session
    interactive du compte donné dès qu'il se connecte, sans mot de passe
    stocké (contrairement à un service, elle hérite donc de ses préférences
    d'impression/pilote configurées manuellement sur ce compte). }
  Params := '/Create /TN "{#AppName}" /TR "' + TaskRun + '" /SC ONLOGON /RU "' +
    Trim(ConfigPage.Values[2]) + '" /IT /RL HIGHEST /F';

  Exec(ExpandConstant('{sys}\schtasks.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ Après l'installation des fichiers : config + tâche planifiée }
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { 1. Générer agent.ini }
    WriteConfigFile();

    { 2. Créer le dossier de logs }
    if not DirExists('{#ConfigDir}\logs') then
      CreateDir('{#ConfigDir}\logs');

    { 3. Retirer un éventuel ancien service (mise à jour depuis l'ancienne
      version de l'installeur qui utilisait NSSM) }
    RemoveLegacyService();

    { 4. Créer la tâche planifiée "à la connexion" }
    CreateLogonTask();

    MsgBox(
      'Installation terminée. L''agent démarrera automatiquement à la ' +
      'prochaine connexion du compte ' + Trim(ConfigPage.Values[2]) + '.' + #13#10 +
      'Pour le démarrer immédiatement sans redémarrer la session, lance-le ' +
      'manuellement depuis le Planificateur de tâches (tâche "PrintAgent").',
      mbInformation, MB_OK
    );
  end;
end;

{ ─── DÉSINSTALLATION ─────────────────────────────────────────────────────── }

{ Avant de supprimer les fichiers : retirer la tâche planifiée (et un
  éventuel ancien service NSSM, si l'agent n'a jamais été mis à jour) }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "{#AppName}" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#AppName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(1000);
    Exec(ExpandConstant('{sys}\sc.exe'), 'delete {#AppName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

; ═══════════════════════════════════════════════════════════════════════════
;  Installeur de l'Agent d'impression d'étiquettes
;  Compile avec Inno Setup Compiler (https://jrsoftware.org/isdl.php)
; ═══════════════════════════════════════════════════════════════════════════

#define AppName "PrintAgent"
#define AppVersion "1.0.0"
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
; Les binaires vont dans le sous-dossier dist\ de l'installation
Source: "printagent.exe"; DestDir: "{#InstallDir}\dist"; Flags: ignoreversion
Source: "nssm.exe"; DestDir: "{#InstallDir}\dist"; Flags: ignoreversion

[Code]
{ ─────────────────────────────────────────────────────────────────────────
  Pascal Script : logique custom de l'installeur.
  On crée une page de saisie personnalisée (URL serveur + token), puis on
  génère agent.ini et on installe le service NSSM à la fin.
  ───────────────────────────────────────────────────────────────────────── }

var
  ConfigPage: TInputQueryWizardPage;

{ Crée la page de saisie custom après la page de sélection du dossier }
procedure InitializeWizard();
begin
  ConfigPage := CreateInputQueryPage(
    wpSelectDir,
    'Configuration de l''agent',
    'Paramètres de connexion au serveur',
    'Renseignez l''URL du serveur et le token de cet agent ' +
    '(copié depuis l''administration Django).'
  );
  { Champ 0 : URL serveur, avec valeur par défaut }
  ConfigPage.Add('URL du serveur (ex: https://mon-serveur.fr) :', False);
  ConfigPage.Values[0] := 'http://localhost:8000';
  { Champ 1 : token. Le 'True' masque la saisie (comme un mot de passe) }
  ConfigPage.Add('Token de l''agent :', False);
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

{ Exécute une commande NSSM et attend la fin }
function RunNssm(Params: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(
    ExpandConstant('{#InstallDir}\dist\nssm.exe'),
    Params,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

{ Après l'installation des fichiers : config + service }
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { 1. Générer agent.ini }
    WriteConfigFile();

    { 2. Créer le dossier de logs }
    if not DirExists('{#ConfigDir}\logs') then
      CreateDir('{#ConfigDir}\logs');

    { 3. Installer le service NSSM (mêmes commandes qu'en manuel) }
    RunNssm('install {#AppName} "{#InstallDir}\dist\printagent.exe"');
    RunNssm('set {#AppName} AppParameters "--config {#ConfigDir}\agent.ini"');
    RunNssm('set {#AppName} AppDirectory "{#InstallDir}\dist"');
    RunNssm('set {#AppName} Start SERVICE_AUTO_START');
    RunNssm('set {#AppName} Description "Agent d''impression d''etiquettes"');
    RunNssm('set {#AppName} AppExit Default Restart');
    RunNssm('set {#AppName} AppStdout "{#ConfigDir}\logs\agent.log"');
    RunNssm('set {#AppName} AppStderr "{#ConfigDir}\logs\agent.log"');
    RunNssm('set {#AppName} AppRotateFiles 1');
    RunNssm('set {#AppName} AppRotateBytes 1048576');

    { 4. Démarrer le service }
    RunNssm('start {#AppName}');
  end;
end;

{ ─── DÉSINSTALLATION ─────────────────────────────────────────────────────── }

{ Avant de supprimer les fichiers : arrêter et retirer le service }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  TempNssm: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { On copie nssm.exe dans un dossier temporaire pour l'utiliser SANS
      verrouiller celui du dossier d'installation (qu'on veut supprimer). }
    TempNssm := ExpandConstant('{tmp}\nssm.exe');
    FileCopy(ExpandConstant('{#InstallDir}\dist\nssm.exe'), TempNssm, False);

    { Arrêter le service et attendre qu'il soit vraiment stoppé }
    Exec(TempNssm, 'stop {#AppName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(2000);

    { Supprimer le service (via la copie temporaire de nssm) }
    Exec(TempNssm, 'remove {#AppName} confirm', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(1000);
  end;
end;
// SPDX-License-Identifier: LGPL-2.1-or-later
// Copyright (C) 2026 G4OCCT Contributors
//
/// @file main.cc
/// @brief G4OCCT simulation runner with CLI argument processing and JSON steering.
///
/// This program loads a STEP geometry file via G4OCCT, runs a Geant4 simulation
/// with the requested particle type and event count, and writes the results to a
/// JSON output file.
///
/// Command-line usage:
///   g4occt_runner --step <geometry.step> \
///                 --type <sim_type>      \
///                 --particle <particle>  \
///                 --events <N>           \
///                 --output <results.json>
///
/// Alternatively, all parameters may be supplied via a JSON steering file:
///   g4occt_runner --config <steering.json>
///
/// Steering file format (all fields optional; defaults shown):
/// @code{.json}
/// {
///   "step":     "geometry.step",
///   "type":     "geantino_scan",
///   "particle": "geantino",
///   "nEvents":  1000,
///   "output":   "results.json"
/// }
/// @endcode
///
/// Supported simulation types:
///   - geantino_scan : Fire massless, non-interacting geantinos isotropically
///                     to probe the geometry without physics interactions.
///
/// Supported particle names: any Geant4 particle name (e.g. "geantino",
///   "e-", "proton", "gamma").  For "geantino_scan" the particle is forced
///   to "geantino" regardless of the --particle setting.
///
/// Output JSON format:
/// @code{.json}
/// {
///   "status": "complete",
///   "type": "geantino_scan",
///   "particle": "geantino",
///   "nEvents": 1000,
///   "step_file": "geometry.step",
///   "total_steps": 5432,
///   "total_edep_MeV": 0.0,
///   "avg_steps_per_event": 5.432
/// }
/// @endcode

// ── C++ standard library ─────────────────────────────────────────────────────
#include <atomic>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <string_view>

// ── nlohmann/json ─────────────────────────────────────────────────────────────
#include <nlohmann/json.hpp>

// ── Geant4 ───────────────────────────────────────────────────────────────────
#include <G4Box.hh>
#include <G4LogicalVolume.hh>
#include <G4NistManager.hh>
#include <G4PVPlacement.hh>
#include <G4ParticleGun.hh>
#include <G4ParticleTable.hh>
#include <G4RandomDirection.hh>
#include <G4RunManagerFactory.hh>
#include <G4Step.hh>
#include <G4SystemOfUnits.hh>
#include <G4UImanager.hh>
#include <G4UserEventAction.hh>
#include <G4UserRunAction.hh>
#include <G4UserSteppingAction.hh>
#include <G4VUserActionInitialization.hh>
#include <G4VUserDetectorConstruction.hh>
#include <G4VUserPrimaryGeneratorAction.hh>
#include <QBBC.hh>

// ── G4OCCT ───────────────────────────────────────────────────────────────────
#include <G4OCCT/G4OCCTSolid.hh>

// ── OpenCASCADE ──────────────────────────────────────────────────────────────
#include <BRep_Builder.hxx>
#include <BRepBndLib.hxx>
#include <Bnd_Box.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Reader.hxx>
#include <TopoDS_Shape.hxx>

// ─────────────────────────────────────────────────────────────────────────────
// Runner configuration
// ─────────────────────────────────────────────────────────────────────────────

struct RunnerConfig {
  std::string step_file   = "geometry.step";
  std::string sim_type    = "geantino_scan";
  std::string particle    = "geantino";
  long long   n_events    = 1000;
  std::string output_file = "results.json";
};

/// Load a RunnerConfig from a JSON file.
RunnerConfig load_config(const std::string& path) {
  std::ifstream ifs(path);
  if (!ifs)
    throw std::runtime_error("Cannot open config file: " + path);
  nlohmann::json j = nlohmann::json::parse(ifs);

  RunnerConfig cfg;
  if (j.contains("step") && j["step"].is_string())
    cfg.step_file = j["step"].get<std::string>();
  if (j.contains("type") && j["type"].is_string())
    cfg.sim_type = j["type"].get<std::string>();
  if (j.contains("particle") && j["particle"].is_string())
    cfg.particle = j["particle"].get<std::string>();
  if (j.contains("nEvents") && j["nEvents"].is_number_integer())
    cfg.n_events = j["nEvents"].get<long long>();
  if (j.contains("output") && j["output"].is_string())
    cfg.output_file = j["output"].get<std::string>();
  return cfg;
}

// ─────────────────────────────────────────────────────────────────────────────
// Accumulated simulation statistics (thread-safe)
// ─────────────────────────────────────────────────────────────────────────────

struct SimStats {
  std::atomic<long long> total_steps{0};
  std::atomic<long long> total_edep_keV{0}; // stored as integer keV × 1000
};

static SimStats g_stats;

// ─────────────────────────────────────────────────────────────────────────────
// Detector construction — loads the STEP file with G4OCCT
// ─────────────────────────────────────────────────────────────────────────────

class RunnerDetectorConstruction : public G4VUserDetectorConstruction {
public:
  explicit RunnerDetectorConstruction(std::string step_file)
      : fStepFile(std::move(step_file)) {}

  G4VPhysicalVolume* Construct() override {
    // ── Load STEP geometry ─────────────────────────────────────────────────
    STEPControl_Reader reader;
    if (reader.ReadFile(fStepFile.c_str()) != IFSelect_RetDone) {
      throw std::runtime_error("Failed to read STEP file: " + fStepFile);
    }
    if (reader.TransferRoots() <= 0) {
      throw std::runtime_error("No STEP roots transferred from: " + fStepFile);
    }
    TopoDS_Shape shape = reader.OneShape();
    if (shape.IsNull()) {
      throw std::runtime_error("Null shape loaded from STEP file: " + fStepFile);
    }

    // ── Compute bounding box for world sizing ──────────────────────────────
    Bnd_Box bbox;
    BRepBndLib::Add(shape, bbox);
    double xmin = 0, ymin = 0, zmin = 0, xmax = 0, ymax = 0, zmax = 0;
    bbox.Get(xmin, ymin, zmin, xmax, ymax, zmax);

    // OCCT units are mm; Geant4 units are also mm by default via G4SystemOfUnits.
    // Add 20 % margin on each side for the world volume.
    const double margin = 0.2;
    const double wx     = std::max(std::abs(xmax - xmin) * (1.0 + margin), 10.0);
    const double wy     = std::max(std::abs(ymax - ymin) * (1.0 + margin), 10.0);
    const double wz     = std::max(std::abs(zmax - zmin) * (1.0 + margin), 10.0);

    // Centre of the STEP geometry in OCCT space.
    const double cx = (xmin + xmax) * 0.5;
    const double cy = (ymin + ymax) * 0.5;
    const double cz = (zmin + zmax) * 0.5;

    // ── Materials ─────────────────────────────────────────────────────────
    G4NistManager* nist   = G4NistManager::Instance();
    G4Material*    matAir = nist->FindOrBuildMaterial("G4_AIR");
    G4Material*    matDet = nist->FindOrBuildMaterial("G4_WATER");

    // ── World ─────────────────────────────────────────────────────────────
    auto* worldSolid = new G4Box("World", wx * mm, wy * mm, wz * mm);
    auto* worldLV    = new G4LogicalVolume(worldSolid, matAir, "World");
    auto* worldPV =
        new G4PVPlacement(nullptr, G4ThreeVector(), worldLV, "World", nullptr, false, 0, true);

    // ── OCCT detector volume ──────────────────────────────────────────────
    auto* detSolid = new G4OCCTSolid("Detector", shape);
    auto* detLV    = new G4LogicalVolume(detSolid, matDet, "Detector");
    // Offset the detector so its geometric centre aligns with the world origin.
    new G4PVPlacement(nullptr, G4ThreeVector(-cx * mm, -cy * mm, -cz * mm), detLV, "Detector",
                      worldLV, false, 0, true);

    return worldPV;
  }

private:
  std::string fStepFile;
};

// ─────────────────────────────────────────────────────────────────────────────
// Primary generator action
// ─────────────────────────────────────────────────────────────────────────────

class RunnerPrimaryGeneratorAction : public G4VUserPrimaryGeneratorAction {
public:
  explicit RunnerPrimaryGeneratorAction(const std::string& particle_name,
                                        const std::string& sim_type)
      : fParticleName(particle_name), fSimType(sim_type) {
    fGun = new G4ParticleGun(1);

    // Resolve particle.  For geantino_scan the particle is always "geantino".
    const std::string effective_particle =
        (fSimType == "geantino_scan") ? "geantino" : fParticleName;

    G4ParticleDefinition* particle =
        G4ParticleTable::GetParticleTable()->FindParticle(effective_particle);
    if (particle == nullptr) {
      // Fall back to geantino if the requested particle is not found.
      std::cerr << "Warning: particle '" << effective_particle
                << "' not found in particle table; using geantino.\n";
      particle = G4ParticleTable::GetParticleTable()->FindParticle("geantino");
    }
    fGun->SetParticleDefinition(particle);
    fGun->SetParticleEnergy(1.0 * GeV);
  }

  ~RunnerPrimaryGeneratorAction() override { delete fGun; }

  void GeneratePrimaries(G4Event* event) override {
    // Isotropic random direction for geometry probing.
    fGun->SetParticleMomentumDirection(G4RandomDirection());
    // Fire from the origin; for a geantino_scan the exact start position
    // is less important than the direction distribution.
    fGun->SetParticlePosition(G4ThreeVector(0, 0, 0));
    fGun->GeneratePrimaryVertex(event);
  }

private:
  std::string   fParticleName;
  std::string   fSimType;
  G4ParticleGun* fGun = nullptr;
};

// ─────────────────────────────────────────────────────────────────────────────
// Stepping action — accumulates per-step statistics
// ─────────────────────────────────────────────────────────────────────────────

class RunnerSteppingAction : public G4UserSteppingAction {
public:
  void UserSteppingAction(const G4Step* step) override {
    g_stats.total_steps.fetch_add(1, std::memory_order_relaxed);
    // Accumulate energy deposit in units of 0.001 keV (i.e. eV) as integer.
    double edep = step->GetTotalEnergyDeposit() / keV;
    g_stats.total_edep_keV.fetch_add(static_cast<long long>(edep * 1000),
                                     std::memory_order_relaxed);
  }
};

// ─────────────────────────────────────────────────────────────────────────────
// Action initialisation
// ─────────────────────────────────────────────────────────────────────────────

class RunnerActionInitialization : public G4VUserActionInitialization {
public:
  RunnerActionInitialization(std::string particle, std::string sim_type)
      : fParticle(std::move(particle)), fSimType(std::move(sim_type)) {}

  void Build() const override {
    SetUserAction(new RunnerPrimaryGeneratorAction(fParticle, fSimType));
    SetUserAction(new RunnerSteppingAction());
  }

  void BuildForMaster() const override {
    // No master-level actions needed.
  }

private:
  std::string fParticle;
  std::string fSimType;
};

// ─────────────────────────────────────────────────────────────────────────────
// JSON output writer
// ─────────────────────────────────────────────────────────────────────────────

void write_results_json(const RunnerConfig& cfg, const std::string& status,
                        const std::string& error_msg = "") {
  long long total_steps    = g_stats.total_steps.load();
  long long total_edep_raw = g_stats.total_edep_keV.load(); // ×1000 keV
  double    total_edep_MeV = static_cast<double>(total_edep_raw) / 1000.0 / 1000.0;
  double    avg_steps =
      cfg.n_events > 0 ? static_cast<double>(total_steps) / static_cast<double>(cfg.n_events)
                       : 0.0;

  nlohmann::json j;
  j["status"]             = status;
  j["type"]               = cfg.sim_type;
  j["particle"]           = cfg.particle;
  j["nEvents"]            = cfg.n_events;
  j["step_file"]          = cfg.step_file;
  j["total_steps"]        = total_steps;
  j["total_edep_MeV"]     = total_edep_MeV;
  j["avg_steps_per_event"] = avg_steps;
  if (!error_msg.empty())
    j["error"] = error_msg;

  std::ofstream ofs(cfg.output_file);
  if (!ofs) {
    throw std::runtime_error("Cannot open output file for writing: " + cfg.output_file);
  }
  ofs << j.dump(2) << "\n";
}

// ─────────────────────────────────────────────────────────────────────────────
// Command-line argument parsing
// ─────────────────────────────────────────────────────────────────────────────

static void print_usage(const char* prog) {
  std::cerr << "Usage:\n";
  std::cerr << "  " << prog << " --step <geometry.step>\n";
  std::cerr << "            [--type geantino_scan]\n";
  std::cerr << "            [--particle geantino]\n";
  std::cerr << "            [--events 1000]\n";
  std::cerr << "            [--output results.json]\n";
  std::cerr << "\n";
  std::cerr << "  " << prog << " --config <steering.json>\n";
  std::cerr << "\n";
  std::cerr << "Options:\n";
  std::cerr << "  --step FILE      Path to input STEP geometry file\n";
  std::cerr << "  --type TYPE      Simulation type: geantino_scan (default)\n";
  std::cerr << "  --particle NAME  Geant4 particle name (default: geantino)\n";
  std::cerr << "  --events N       Number of primary events (default: 1000)\n";
  std::cerr << "  --output FILE    Path for JSON results output (default: results.json)\n";
  std::cerr << "  --config FILE    JSON steering file (replaces individual options)\n";
  std::cerr << "  --help           Show this message\n";
}

static RunnerConfig parse_args(int argc, char** argv) {
  RunnerConfig cfg;
  std::string  config_file;

  for (int i = 1; i < argc; ++i) {
    std::string_view arg = argv[i];

    auto next = [&]() -> std::string {
      if (i + 1 >= argc) {
        throw std::runtime_error(std::string("Missing value for option: ") + std::string(arg));
      }
      return argv[++i];
    };

    if (arg == "--help" || arg == "-h") {
      print_usage(argv[0]);
      std::exit(0);
    } else if (arg == "--config") {
      config_file = next();
    } else if (arg == "--step") {
      cfg.step_file = next();
    } else if (arg == "--type") {
      cfg.sim_type = next();
    } else if (arg == "--particle") {
      cfg.particle = next();
    } else if (arg == "--events") {
      std::string val = next();
      char*       end = nullptr;
      errno           = 0;
      long long n     = std::strtoll(val.c_str(), &end, 10);
      if (errno != 0 || end == val.c_str() || *end != '\0' || n <= 0) {
        throw std::runtime_error("Invalid value for --events: " + val);
      }
      cfg.n_events = n;
    } else if (arg == "--output") {
      cfg.output_file = next();
    } else {
      throw std::runtime_error(std::string("Unknown option: ") + std::string(arg));
    }
  }

  // If a config file was given, load it and override any individual options
  // that were also specified on the command line (CLI takes precedence over
  // the defaults in the file, but the file provides the base configuration).
  if (!config_file.empty()) {
    RunnerConfig from_file = load_config(config_file);
    // Override file values with explicit CLI values where the CLI value
    // differs from the struct default (a simplification: CLI always wins).
    if (cfg.step_file == "geometry.step" && !from_file.step_file.empty())
      cfg.step_file = from_file.step_file;
    if (cfg.sim_type == "geantino_scan" && !from_file.sim_type.empty())
      cfg.sim_type = from_file.sim_type;
    if (cfg.particle == "geantino" && !from_file.particle.empty())
      cfg.particle = from_file.particle;
    if (cfg.n_events == 1000 && from_file.n_events != 1000)
      cfg.n_events = from_file.n_events;
    if (cfg.output_file == "results.json" && !from_file.output_file.empty())
      cfg.output_file = from_file.output_file;
  }

  return cfg;
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────

int main(int argc, char** argv) {
  RunnerConfig cfg;
  try {
    cfg = parse_args(argc, argv);
  } catch (const std::exception& ex) {
    std::cerr << "Error: " << ex.what() << "\n\n";
    print_usage(argv[0]);
    return 1;
  }

  // Validate that the STEP file exists before starting Geant4.
  if (!std::filesystem::exists(cfg.step_file)) {
    std::cerr << "Error: STEP file not found: " << cfg.step_file << "\n";
    return 1;
  }

  // ── Geant4 run manager ────────────────────────────────────────────────────
  auto* runManager = G4RunManagerFactory::CreateRunManager(G4RunManagerType::Default);

  // ── User initializations ──────────────────────────────────────────────────
  runManager->SetUserInitialization(new RunnerDetectorConstruction(cfg.step_file));
  runManager->SetUserInitialization(new QBBC(0));
  runManager->SetUserInitialization(
      new RunnerActionInitialization(cfg.particle, cfg.sim_type));

  // ── Run the simulation ────────────────────────────────────────────────────
  G4UImanager* ui = G4UImanager::GetUIpointer();

  std::string error_msg;
  std::string status = "complete";
  try {
    if (ui->ApplyCommand("/run/initialize") != 0) {
      throw std::runtime_error("/run/initialize failed");
    }
    const std::string beam_on = "/run/beamOn " + std::to_string(cfg.n_events);
    if (ui->ApplyCommand(beam_on) != 0) {
      throw std::runtime_error(beam_on + " failed");
    }
  } catch (const std::exception& ex) {
    status    = "error";
    error_msg = ex.what();
    std::cerr << "Simulation error: " << error_msg << "\n";
  }

  delete runManager;

  // ── Write results ─────────────────────────────────────────────────────────
  try {
    write_results_json(cfg, status, error_msg);
  } catch (const std::exception& ex) {
    std::cerr << "Failed to write results: " << ex.what() << "\n";
    return 1;
  }

  return (status == "complete") ? 0 : 1;
}

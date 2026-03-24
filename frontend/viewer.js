// SPDX-License-Identifier: LGPL-2.1-or-later
// Copyright (C) 2026 G4OCCT Contributors
//
// Three.js-based 3D geometry viewer for G4OCCT.
// Loaded as a <script type="module"> from index.html.
// All viewer logic is self-contained here; app.js is not a dependency.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const ctx = window.G4OCCT_CONTEXT || {};

let _scene, _camera, _renderer, _controls, _currentModel;

// ── Viewer initialisation ──────────────────────────────────────────────────

function initViewer(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;

  // Scene
  _scene = new THREE.Scene();
  _scene.background = new THREE.Color(0x1a1a2e);

  // Camera
  const w = container.clientWidth || 800;
  const h = container.clientHeight || 400;
  _camera = new THREE.PerspectiveCamera(45, w / h, 0.01, 10000);
  _camera.position.set(0, 0, 5);

  // Renderer
  _renderer = new THREE.WebGLRenderer({ antialias: true });
  _renderer.setPixelRatio(window.devicePixelRatio);
  _renderer.setSize(w, h);
  container.appendChild(_renderer.domElement);

  // Lights — ambient + two directional lights for good coverage
  const ambient = new THREE.AmbientLight(0xffffff, 0.6);
  _scene.add(ambient);
  const dir1 = new THREE.DirectionalLight(0xffffff, 1.0);
  dir1.position.set(5, 10, 7.5);
  _scene.add(dir1);
  const dir2 = new THREE.DirectionalLight(0xffffff, 0.3);
  dir2.position.set(-5, -5, -5);
  _scene.add(dir2);

  // OrbitControls
  _controls = new OrbitControls(_camera, _renderer.domElement);
  _controls.enableDamping = true;
  _controls.dampingFactor = 0.05;

  // Render loop
  function animate() {
    requestAnimationFrame(animate);
    _controls.update();
    _renderer.render(_scene, _camera);
  }
  animate();

  // Resize handler
  window.addEventListener("resize", () => {
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    _camera.aspect = cw / ch;
    _camera.updateProjectionMatrix();
    _renderer.setSize(cw, ch);
  });
}

// ── GPU resource disposal ──────────────────────────────────────────────────

function _disposeMaterial(material) {
  if (!material || typeof material.dispose !== "function") return;
  for (const key in material) {
    const value = material[key];
    if (value && value.isTexture && typeof value.dispose === "function") {
      value.dispose();
    }
  }
  material.dispose();
}

function _disposeModel(model) {
  if (!model) return;
  model.traverse((child) => {
    if (child.isMesh || child.isPoints || child.isLine) {
      if (child.geometry && typeof child.geometry.dispose === "function") {
        child.geometry.dispose();
      }
      const mat = child.material;
      if (Array.isArray(mat)) {
        mat.forEach(_disposeMaterial);
      } else if (mat) {
        _disposeMaterial(mat);
      }
    }
  });
}

// ── glTF loading ───────────────────────────────────────────────────────────

function loadGltf(url) {
  // Remove and dispose previous model from scene to free GPU memory
  if (_currentModel) {
    _scene.remove(_currentModel);
    _disposeModel(_currentModel);
    _currentModel = null;
  }

  return new Promise((resolve, reject) => {
    const loader = new GLTFLoader();
    loader.load(
      url,
      (gltf) => {
        _currentModel = gltf.scene;
        _scene.add(_currentModel);

        // Auto-fit camera to the model's bounding box
        const box = new THREE.Box3().setFromObject(_currentModel);
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z);
        const fov = _camera.fov * (Math.PI / 180);
        const dist = Math.abs(maxDim / (2 * Math.tan(fov / 2)));

        _camera.position.set(center.x, center.y, center.z + dist * 1.5);
        _camera.near = dist / 100;
        _camera.far = dist * 100;
        _camera.updateProjectionMatrix();
        _controls.target.copy(center);
        _controls.update();

        resolve(gltf);
      },
      undefined,
      reject,
    );
  });
}

// ── Button handler ─────────────────────────────────────────────────────────

// Module scripts are deferred — the DOM is ready by the time this runs.
initViewer("viewer-container");

const _btn = document.getElementById("load-geometry-btn");
const _overlay = document.getElementById("viewer-loading");

if (_btn) {
  _btn.addEventListener("click", async () => {
    if (!ctx.documentId || !ctx.workspaceId || !ctx.elementId) {
      alert(
        "No document context available. Open this app from an Onshape document tab.",
      );
      return;
    }

    _btn.disabled = true;
    if (_overlay) _overlay.classList.remove("hidden");

    // Read the current element-type selection from the shared form field.
    const elementTypeSelect = document.getElementById("element-type-select");
    const elementType = elementTypeSelect ? elementTypeSelect.value : "partstudio";

    const params = new URLSearchParams({
      documentId: ctx.documentId,
      workspaceId: ctx.workspaceId,
      elementId: ctx.elementId,
      elementType,
    });

    try {
      // The export endpoint is POST-only; fetch the GLB binary and hand it to
      // GLTFLoader via a temporary object URL to avoid a GET 405 error.
      const response = await fetch(`/api/element/export-gltf?${params}`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(
          `Server returned ${response.status} ${response.statusText}`,
        );
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      try {
        await loadGltf(objectUrl);
      } finally {
        URL.revokeObjectURL(objectUrl);
      }
    } catch (err) {
      console.error("Failed to load glTF geometry:", err);
      alert(`Failed to load geometry: ${err.message || String(err)}`);
    } finally {
      _btn.disabled = false;
      if (_overlay) _overlay.classList.add("hidden");
    }
  });
}

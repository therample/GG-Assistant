
/**
 * GeoGuessr Assistant — Frontend
 * ═══════════════════════════════════════════════════════════════
 * Handles: Leaflet map, API polling, settings, animations.
 */

'use strict';

// ═══════════════ CONFIG ═══════════════
const CONFIG = {
    apiBase: '',
    cartoApiKey: '$$',   // ← вставьте сюда
    tileProviders: {
        dark: {
            url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
            carto: true,              // ← флаг «это CARTO»
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
            subdomains: 'abcd',
            maxZoom: 20
        },
        // osm, satellite, topo — без изменений, ключ им не нужен
        voyager: {
            url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
            carto: true,              // ← флаг «это CARTO»
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
            subdomains: 'abcd',
            maxZoom: 20
        },
        osm: {
            url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            subdomains: 'abc',
            maxZoom: 19
        },
        satellite: {
            url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye',
            maxZoom: 18
        },
        topo: {
            url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, <a href="https://opentopomap.org">SRTM</a>',
            subdomains: 'abc',
            maxZoom: 17
        }
    }
};

// ═══════════════ STATE ═══════════════
const state = {
    map: null,
    marker: null,
    tileLayer: null,
    currentLocation: null,
    countryInfo: null,
    connected: false,
    settings: {
        mapProvider: 'dark',
        autoZoom: true,
        autoShow: true,
        reverseGeocode: true,
        pollInterval: 500
    },
    lastLocationHash: null
};

// ═══════════════ DOM ELEMENTS ═══════════════
const $ = id => document.getElementById(id);

const elements = {
    statusDot: $('statusDot'),
    statusText: $('statusText'),
    roundText: $('roundText'),
    latValue: $('latValue'),
    lngValue: $('lngValue'),
    locationCard: $('locationCard'),
    locationPulse: $('locationPulse'),
    countryFlag: $('countryFlag'),
    countryName: $('countryName'),
    countryCode: $('countryCode'),
    countryInfo: $('countryInfo'),
    sourceValue: $('sourceValue'),
    panoIdValue: $('panoIdValue'),
    lastUpdateValue: $('lastUpdateValue'),
    mapOverlay: $('mapOverlay'),
    toastContainer: $('toastContainer'),
    settingsModal: $('settingsModal')
};

// ═══════════════ MAP INITIALIZATION ═══════════════
function initMap() {
    state.map = L.map('map', {
        center: [20, 0],
        zoom: 2,
        zoomControl: true,
        attributionControl: true,
        worldCopyJump: true
    });

    setTileProvider(state.settings.mapProvider);
}

function setTileProvider(provider) {
    if (state.tileLayer) {
        state.map.removeLayer(state.tileLayer);
    }

    const config = CONFIG.tileProviders[provider] || CONFIG.tileProviders.dark;
    let url = config.url;

    // Добавляем ключ только к CARTO-тайлам
    if (config.carto && CONFIG.cartoApiKey) {
        url += (url.includes('?') ? '&' : '?') +
               'key=' + encodeURIComponent(CONFIG.cartoApiKey);
    }

    state.tileLayer = L.tileLayer(url, {
        attribution: config.attribution,
        subdomains: config.subdomains || 'abc',
        maxZoom: config.maxZoom
    }).addTo(state.map);
}

// ═══════════════ MARKER ═══════════════
function createMarkerIcon() {
    return L.divIcon({
        className: 'gg-marker-wrapper',
        html: '<div class="gg-marker"></div>',
        iconSize: [24, 24],
        iconAnchor: [12, 12]
    });
}

function placeMarker(lat, lng, zoom = true) {
    if (state.marker) {
        state.map.removeLayer(state.marker);
    }

    state.marker = L.marker([lat, lng], {
        icon: createMarkerIcon()
    }).addTo(state.map);

    if (zoom || state.settings.autoZoom) {
        state.map.flyTo([lat, lng], Math.max(state.map.getZoom(), 5), {
            duration: 1.5,
            easeLinearity: 0.25
        });
    }

    state.marker.bindPopup(`
        <div style="font-family: Inter; text-align: center;">
            <div style="font-size: 12px; color: #888; margin-bottom: 4px;">Target Location</div>
            <div style="font-family: JetBrains Mono; font-size: 14px; font-weight: 600;">
                ${lat.toFixed(6)}, ${lng.toFixed(6)}
            </div>
        </div>
    `);
}// ═══════════════ 5K MARKER (в игре) ═══════════════
async function place5KInGame() {
    if (!state.currentLocation) {
        showToast('No location data yet', 'error');
        return;
    }
    const { lat, lng } = state.currentLocation;
    try {
        const resp = await fetch(`${CONFIG.apiBase}/api/place-5k`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lat, lng })
        });
        const data = await resp.json();
        if (data.success) {
            showToast(`5K marker placed in game (${data.placed})`, 'success');
        } else {
            let msg = data.error || 'failed';
            if (data.reason === 'no-map') {
                msg = 'Game map not captured — zoom/pan the guess map once, then retry';
            }
            showToast(`5K: ${msg}`, 'warning');
        }
    } catch (e) {
        showToast('Backend unavailable', 'error');
    }
}

// ═══════════════ API CALLS ═══════════════
async function fetchStatus() {
    try {
        const resp = await fetch(`${CONFIG.apiBase}/api/status`);
        const data = await resp.json();
        updateConnectionStatus(data.connected);
        return data;
    } catch (e) {
        updateConnectionStatus(false);
        return null;
    }
}

async function fetchLocation() {
    try {
        const resp = await fetch(`${CONFIG.apiBase}/api/location`);
        const data = await resp.json();
        return data;
    } catch (e) {
        return null;
    }
}

async function reverseGeocode(lat, lng) {
    try {
        const resp = await fetch(
            `${CONFIG.apiBase}/api/reverse-geocode?lat=${lat}&lng=${lng}`
        );
        const data = await resp.json();
        return data;
    } catch (e) {
        return null;
    }
}

// ═══════════════ UI UPDATES ═══════════════
function updateConnectionStatus(connected) {
    if (state.connected === connected) return;
    state.connected = connected;

    elements.statusDot.className =
        `status-dot ${connected ? 'connected' : 'disconnected'}`;
    elements.statusText.textContent = connected ? 'Connected' : 'Disconnected';
}

function updateLocationDisplay(data) {
    const location = data.location;
    if (!location) return;

    const lat = location.lat;
    const lng = location.lng;
    const hash = `${lat.toFixed(8)}:${lng.toFixed(8)}`;

    // Skip if same location
    if (hash === state.lastLocationHash) {
        updateTimestamp(data);
        return;
    }

    state.lastLocationHash = hash;
    state.currentLocation = location;

    // Animate coordinate update
    animateValueChange(elements.latValue, lat.toFixed(6));
    animateValueChange(elements.lngValue, lng.toFixed(6));

    elements.latValue.classList.add('active');
    elements.lngValue.classList.add('active');

    // Card states
    elements.locationCard.classList.add('active');
    elements.locationPulse.classList.add('active');

    // Flash animation
    const coordGroups = document.querySelectorAll('.coord-group');
    coordGroups.forEach(g => {
        g.classList.remove('updated');
        void g.offsetWidth; // force reflow
        g.classList.add('updated');
    });

    // Round number
    if (data.round) {
        elements.roundText.textContent = `Round ${data.round}`;
    }

    // Source
    if (data.source) {
        elements.sourceValue.textContent = data.source;
    }

    // Pano ID
    if (data.panoId) {
        elements.panoIdValue.textContent =
            data.panoId.substring(0, 20) + (data.panoId.length > 20 ? '...' : '');
    }

    // Place on map
    if (state.settings.autoShow) {
        placeMarker(lat, lng);
        elements.mapOverlay.classList.add('hidden');
    }

    // Reverse geocode
    if (state.settings.reverseGeocode) {
        reverseGeocode(lat, lng).then(result => {
            if (result && result.success) {
                updateCountryInfo(result);
            }
        });
    }

    // Toast notification
    showToast(`Location found: ${lat.toFixed(4)}, ${lng.toFixed(4)}`, 'success');

    updateTimestamp(data);
}

function animateValueChange(element, newText) {
    element.style.opacity = '0';
    element.style.transform = 'translateY(-4px)';

    setTimeout(() => {
        element.textContent = newText;
        element.style.transition = 'opacity 0.3s, transform 0.3s';
        element.style.opacity = '1';
        element.style.transform = 'translateY(0)';
    }, 150);
}

function updateTimestamp(data) {
    if (data.timestamp) {
        const date = new Date(data.timestamp * 1000);
        elements.lastUpdateValue.textContent =
            date.toLocaleTimeString();
    }
}

function updateCountryInfo(geocodeResult) {
    const { country, country_code, display_name } = geocodeResult;

    if (country_code) {
        elements.countryFlag.innerHTML = `
            <img src="https://flagcdn.com/w320/${country_code}.png"
                 alt="${country}"
                 onerror="this.parentElement.innerHTML='<div class=flag-placeholder>—</div>'">
        `;
    }

    elements.countryName.textContent = country || 'Unknown';
    elements.countryCode.textContent = country_code?.toUpperCase() || '—';
    elements.countryInfo.classList.add('detected');
    state.countryInfo = geocodeResult;
}

// ═══════════════ TOAST ═══════════════
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;

    elements.toastContainer.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('fade-out');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ═══════════════ SETTINGS ═══════════════
function loadSettings() {
    const saved = localStorage.getItem('gg_settings');
    if (saved) {
        try {
            state.settings = { ...state.settings, ...JSON.parse(saved) };
        } catch (e) {}
    }
    applySettingsToUI();
}

function saveSettings() {
    localStorage.setItem('gg_settings', JSON.stringify(state.settings));
    applySettings();
}

function applySettingsToUI() {
    $('setMapProvider').value = state.settings.mapProvider;
    $('setAutoZoom').checked = state.settings.autoZoom;
    $('setAutoShow').checked = state.settings.autoShow;
    $('setReverseGeocode').checked = state.settings.reverseGeocode;
    $('setPollInterval').value = state.settings.pollInterval;
}

function applySettings() {
    setTileProvider(state.settings.mapProvider);
    CONFIG.pollInterval = state.settings.pollInterval;
}

function collectSettingsFromUI() {
    state.settings.mapProvider = $('setMapProvider').value;
    state.settings.autoZoom = $('setAutoZoom').checked;
    state.settings.autoShow = $('setAutoShow').checked;
    state.settings.reverseGeocode = $('setReverseGeocode').checked;
    state.settings.pollInterval = parseInt($('setPollInterval').value) || 500;
}

// ═══════════════ EVENT HANDLERS ═══════════════
function setupEventListeners() {
    // Settings modal
    $('btnSettings').addEventListener('click', () => {
        elements.settingsModal.classList.add('open');
    });

    $('btnCloseSettings').addEventListener('click', () => {
        elements.settingsModal.classList.remove('open');
    });

    elements.settingsModal.addEventListener('click', (e) => {
        if (e.target === elements.settingsModal) {
            elements.settingsModal.classList.remove('open');
        }
    });
        // 5K marker в игре
    $('btnMark5K').addEventListener('click', (e) => {
        if (e.shiftKey) {
            fetch(`${CONFIG.apiBase}/api/clear-5k`, { method: 'POST' });
            showToast('5K markers cleared', 'info');
            return;
        }
        place5KInGame();
    });

    document.addEventListener('keydown', (e) => {
        if (e.repeat) return;
        const tag = document.activeElement?.tagName;
        if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
        if (e.key === '5' && !elements.settingsModal.classList.contains('open')) {
            place5KInGame();
        }
    });
    $('btnSaveSettings').addEventListener('click', () => {
        collectSettingsFromUI();
        saveSettings();
        elements.settingsModal.classList.remove('open');
        showToast('Settings saved', 'success');
    });

    $('btnResetSettings').addEventListener('click', () => {
        state.settings = {
            mapProvider: 'dark',
            autoZoom: true,
            autoShow: true,
            reverseGeocode: true,
            pollInterval: 500
        };
        applySettingsToUI();
        applySettings();
        showToast('Settings reset', 'info');
    });

    // Copy coordinates
    $('btnCopyCoords').addEventListener('click', async () => {
        if (!state.currentLocation) {
            showToast('No location data', 'error');
            return;
        }

        const coords =
            `${state.currentLocation.lat}, ${state.currentLocation.lng}`;
        try {
            await navigator.clipboard.writeText(coords);
            showToast('Coordinates copied!', 'success');
        } catch (e) {
            // Fallback
            const textarea = document.createElement('textarea');
            textarea.value = coords;
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            textarea.remove();
            showToast('Coordinates copied!', 'success');
        }
    });

    // Show on map button
    $('btnShowOnMap').addEventListener('click', () => {
        if (!state.currentLocation) {
            showToast('No location data', 'error');
            return;
        }
        placeMarker(
            state.currentLocation.lat,
            state.currentLocation.lng,
            true
        );
        elements.mapOverlay.classList.add('hidden');
    });

    // Open panorama link
    $('btnOpenPanorama').addEventListener('click', () => {
        if (!state.currentLocation) {
            showToast('No location data', 'error');
            return;
        }

        const { lat, lng } = state.currentLocation;
        const url =
            `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lng}`;
        window.open(url, '_blank');
    });

    // Open Street View
    $('btnOpenStreetView').addEventListener('click', () => {
        if (!state.currentLocation) {
            showToast('No location data', 'error');
            return;
        }

        const { lat, lng } = state.currentLocation;
        const url =
            `https://www.google.com/maps?q=${lat},${lng}&layer=c&cbll=${lat},${lng}`;
        window.open(url, '_blank');
    });

    // Map click for coordinate inspection
    state.map.on('click', (e) => {
        console.log('[GG] Map click:', e.latlng.lat, e.latlng.lng);
    });
}

// ═══════════════ POLLING LOOP ═══════════════
async function pollLoop() {
    // Fetch status
    await fetchStatus();

    // Fetch location
    const data = await fetchLocation();
    if (data && data.location) {
        updateLocationDisplay(data);
    }

    // Schedule next poll
    setTimeout(pollLoop, state.settings.pollInterval);
}

// ═══════════════ INITIALIZATION ═══════════════
function init() {
    console.log('[GG] Initializing...');

    loadSettings();
    initMap();
    setupEventListeners();

    // Start polling
    pollLoop();

    // Check connection periodically
    setInterval(fetchStatus, 5000);

    console.log('[GG] Ready');
}

// Start when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
/**
 * Audio Source Module
 * 
 * Shows fixed UDP recognition status for the UDP-only add-on.
 */

import {
    getAudioRecognitionConfig,
    setAudioRecognitionConfig,
    getAudioRecognitionDevices,
    startAudioRecognition,
    stopAudioRecognition,
    getAudioRecognitionStatus
} from './api.js';

import { showToast } from './dom.js';

// =============================================================================
// State
// =============================================================================

let isModalOpen = false;
let pollInterval = null;
let currentConfig = null;
let isActive = false;
let isFrontendCapture = false; // Deprecated compatibility flag; frontend mic is disabled
let currentTrackSource = null; // Default: no source (shows Idle)
let lastKnownProvider = null; // Last known recognition provider (prevents flashing)

// DOM Elements (cached on init)
let elements = {};

// =============================================================================
// DOM Cache
// =============================================================================

function cacheElements() {
    elements = {
        // Button
        sourceToggle: document.getElementById('source-toggle'),
        sourceName: document.getElementById('source-name'),

        // Modal
        modal: document.getElementById('audio-source-modal'),
        closeBtn: document.getElementById('audio-source-close'),

        // Status
        recognitionStatus: document.getElementById('recognition-status'),
        recognitionMode: document.getElementById('recognition-mode'),
        attemptRow: document.getElementById('attempt-row'),
        recognitionAttempts: document.getElementById('recognition-attempts'),
        lastMatchRow: document.getElementById('last-match-row'),
        lastMatchInfo: document.getElementById('last-match-info'),
        enrichmentRow: document.getElementById('enrichment-row'),
        enrichmentStatus: document.getElementById('enrichment-status'),

        // Quick start
        quickStartBackend: document.getElementById('quick-start-backend'),
        quickStartBackendBtn: document.getElementById('quick-start-backend-btn'),
        backendDeviceName: document.getElementById('backend-device-name'),
        quickStartFrontend: document.getElementById('quick-start-frontend'),
        quickStartFrontendBtn: document.getElementById('quick-start-frontend-btn'),
        quickStartUdp: document.getElementById('quick-start-udp'),
        quickStartUdpBtn: document.getElementById('quick-start-udp-btn'),

        // Device selection
        deviceSelect: document.getElementById('device-select'),
        sampleRateInfo: document.getElementById('sample-rate-info'),
        httpsWarning: document.getElementById('https-warning'),

        // Audio level meter (large)
        audioLevelContainer: document.getElementById('audio-level-container'),
        audioLevelFill: document.getElementById('audio-level-fill'),
        audioLevelValue: document.getElementById('audio-level-value'),

        // Control button (single toggle)
        toggleBtn: document.getElementById('recognition-toggle'),

        // Current song
        currentSongInfo: document.getElementById('current-song-info'),
        currentSongTitle: document.getElementById('current-song-title'),
        currentSongArtist: document.getElementById('current-song-artist'),

        // Advanced settings
        advancedToggle: document.getElementById('advanced-toggle'),
        advancedContent: document.getElementById('advanced-content'),
        recognitionInterval: document.getElementById('recognition-interval'),
        recognitionIntervalValue: document.getElementById('recognition-interval-value'),
        captureDuration: document.getElementById('capture-duration'),
        captureDurationValue: document.getElementById('capture-duration-value'),
        latencyOffset: document.getElementById('latency-offset'),
        latencyOffsetValue: document.getElementById('latency-offset-value'),
        silenceThreshold: document.getElementById('silence-threshold'),
        silenceThresholdValue: document.getElementById('silence-threshold-value'),
    };
}

// =============================================================================
// Modal Control
// =============================================================================

function openModal() {
    if (!elements.modal) return;

    isModalOpen = true;
    elements.modal.classList.add('visible');

    // Load data
    loadDevices();
    loadConfig();
    refreshStatus();

    // Start polling (faster when modal open)
    startPolling(2000);
}

function closeModal() {
    if (!elements.modal) return;

    isModalOpen = false;
    elements.modal.classList.remove('visible');

    // Slow down polling
    startPolling(5000);
}

function toggleAdvanced() {
    const toggle = elements.advancedToggle;
    const content = elements.advancedContent;

    if (toggle && content) {
        toggle.classList.toggle('open');
        content.classList.toggle('open');
    }
}

// =============================================================================
// Device Loading
// =============================================================================

async function loadDevices() {
    const select = elements.deviceSelect;
    if (!select) return;

    try {
        const result = await getAudioRecognitionDevices();
        const udp = (result.devices || [])[0];
        select.innerHTML = '';
        const opt = document.createElement('option');
        opt.value = 'udp';
        opt.textContent = udp?.name || 'UDP audio';
        select.appendChild(opt);
        select.value = 'udp';
    } catch (error) {
        console.error('Error loading UDP source:', error);
    }
}

async function loadConfig() {
    try {
        const result = await getAudioRecognitionConfig();

        if (result.error) {
            console.warn('Failed to load config:', result.error);
            return;
        }

        currentConfig = result.config || {};

        // Update slider values from backend config
        if (elements.recognitionInterval) {
            elements.recognitionInterval.value = currentConfig.recognition_interval || 5;
        }
        if (elements.captureDuration) {
            elements.captureDuration.value = currentConfig.capture_duration || 5;
        }
        if (elements.latencyOffset) {
            elements.latencyOffset.value = currentConfig.latency_offset || 0;
        }
        if (elements.silenceThreshold) {
            elements.silenceThreshold.value = currentConfig.silence_threshold || 500;
        }

        // Update slider value displays by dispatching input events
        elements.recognitionInterval?.dispatchEvent(new Event('input'));
        elements.captureDuration?.dispatchEvent(new Event('input'));
        elements.latencyOffset?.dispatchEvent(new Event('input'));
        elements.silenceThreshold?.dispatchEvent(new Event('input'));

    } catch (error) {
        console.error('Error loading config:', error);
    }
}

// =============================================================================
// Status Polling
// =============================================================================

async function refreshStatus() {
    try {
        const result = await getAudioRecognitionStatus();

        if (result.error) {
            updateStatusDisplay({ active: false, state: 'error' });
            return;
        }

        isActive = result.active || false;

        // Also fetch current track to get the actual source if audio rec is inactive
        // Fix 3.1: Correct endpoint is /current-track, not /api/track/current
        if (!isActive) {
            try {
                const response = await fetch('/current-track');
                const trackData = await response.json();
                if (trackData && trackData.source) {
                    currentTrackSource = trackData.source;
                }
            } catch (e) {
                // Ignore errors fetching track
            }
        }

        updateStatusDisplay(result);
        updateButtonState();

        // Audio level is now updated inline in updateStatusDisplay via audioLevelRow

    } catch (error) {
        console.error('Error refreshing status:', error);
    }
}

function updateButtonState() {
    if (elements.toggleBtn) {
        elements.toggleBtn.textContent = isActive ? 'Stop' : 'Start';
        elements.toggleBtn.classList.toggle('active', isActive);
    }
}

function updateStatusDisplay(status) {
    // Update status text
    if (elements.recognitionStatus) {
        const state = status.state || (status.active ? 'active' : 'idle');
        let displayState = capitalizeFirst(state);

        // Add attempt count if searching
        if (status.consecutive_no_match > 0 && state !== 'idle') {
            displayState = `Searching (${status.consecutive_no_match})`;
        }

        elements.recognitionStatus.textContent = displayState;
        elements.recognitionStatus.className = 'status-value ' + state;
    }

    // Update mode
    if (elements.recognitionMode) {
        const mode = status.mode || '—';
        elements.recognitionMode.textContent = capitalizeFirst(mode);
    }

    // Update attempt count row
    if (elements.attemptRow && elements.recognitionAttempts) {
        if (status.active && status.consecutive_no_match !== undefined) {
            elements.attemptRow.style.display = 'flex';
            const result = status.last_attempt_result || 'idle';
            if (result === 'matched') {
                elements.recognitionAttempts.textContent = '✓ Matched';
                elements.recognitionAttempts.className = 'status-value enriched';
            } else if (result === 'no_match') {
                elements.recognitionAttempts.textContent = `No match (${status.consecutive_no_match})`;
                elements.recognitionAttempts.className = 'status-value no-match';
            } else {
                elements.recognitionAttempts.textContent = capitalizeFirst(result);
                elements.recognitionAttempts.className = 'status-value';
            }
        } else {
            elements.attemptRow.style.display = 'none';
        }
    }

    // Audio level is now handled by large meter in updateButtonState
    // Update amplitude display if active
    if (status.active && status.audio_level !== undefined) {
        updateAudioLevel(status.audio_level);
    }

    // Update last match info
    if (elements.lastMatchRow && elements.lastMatchInfo) {
        if (status.current_song && status.current_song.title) {
            elements.lastMatchRow.style.display = 'flex';
            const song = status.current_song;
            elements.lastMatchInfo.textContent = `${song.artist} - ${song.title}`;
        } else {
            elements.lastMatchRow.style.display = 'none';
        }
    }

    // Update enrichment status
    if (elements.enrichmentRow && elements.enrichmentStatus) {
        if (status.current_song && status.current_song.album_art_url) {
            elements.enrichmentRow.style.display = 'flex';
            elements.enrichmentStatus.textContent = '☑ Metadata';
            elements.enrichmentStatus.className = 'status-value enriched';
        } else {
            elements.enrichmentRow.style.display = 'none';
        }
    }

    // Update button text - source is fixed to UDP in this add-on.
    if (elements.sourceName) {
        elements.sourceName.textContent = status.active ? 'UDP' : 'UDP Idle';
    }

    // Toggle recording indicator on source button
    if (elements.sourceToggle) {
        if (isActive) {
            elements.sourceToggle.classList.add('recording');
        } else {
            elements.sourceToggle.classList.remove('recording');
        }
    }
}

function startPolling(intervalMs) {
    if (pollInterval) {
        clearInterval(pollInterval);
    }
    pollInterval = setInterval(refreshStatus, intervalMs);
}

function stopPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }
}

// =============================================================================
// Recognition Control
// =============================================================================

async function handleStart(mode = 'udp') {
    const configUpdate = {
        enabled: true,
        mode: 'udp'
    };

    if (elements.recognitionInterval) {
        configUpdate.recognition_interval = parseFloat(elements.recognitionInterval.value);
    }
    if (elements.captureDuration) {
        configUpdate.capture_duration = parseFloat(elements.captureDuration.value);
    }
    if (elements.latencyOffset) {
        configUpdate.latency_offset = parseFloat(elements.latencyOffset.value);
    }
    if (elements.silenceThreshold) {
        configUpdate.silence_threshold = parseInt(elements.silenceThreshold.value, 10);
    }

    try {
        await setAudioRecognitionConfig(configUpdate);
        const result = await startAudioRecognition();
        if (result.error) {
            console.error('Failed to confirm UDP recognition:', result.error);
            return;
        }
        await refreshStatus();
    } catch (error) {
        console.error('Error confirming UDP recognition:', error);
    }
}

async function handleStop() {
    try {
        const result = await stopAudioRecognition();
        if (result.error) {
            console.error('Failed to update UDP recognition state:', result.error);
        }
        lastKnownProvider = null;
        await refreshStatus();
    } catch (error) {
        console.error('Error updating UDP recognition state:', error);
    }
}

// =============================================================================
// Device Selection
// =============================================================================

function handleDeviceChange() {
    // UDP source is fixed; no local device selection is available.
}

function isSecureContext() {
    return true;
}

function showHttpsWarning() {
    // No-op: browser microphone input is disabled.
}

function hideHttpsWarning() {
    // No-op: browser microphone input is disabled.
}

function capitalizeFirst(str) {
    if (!str) return '';
    return str.charAt(0).toUpperCase() + str.slice(1).toLowerCase();
}

// =============================================================================
// Audio Level Meter
// =============================================================================

export function updateAudioLevel(level) {
    // Level is normalized 0-1, update fill bar
    if (elements.audioLevelFill) {
        const percent = Math.min(100, Math.max(0, level * 100));
        elements.audioLevelFill.style.width = `${percent}%`;
    }

    // Show raw amplitude value (reverse the normalization: level * 32768 / 2)
    // This matches what silence threshold expects (50-500 range typical)
    if (elements.audioLevelValue) {
        const rawAmplitude = Math.round(level * 32768 / 2);
        elements.audioLevelValue.textContent = `Amp: ${rawAmplitude}`;
    }
}

// =============================================================================
// Initialization
// =============================================================================

export function init() {
    cacheElements();

    if (!elements.sourceToggle) {
        console.log('Audio source UI not found, skipping init');
        return;
    }

    // Button click -> open modal
    elements.sourceToggle.addEventListener('click', openModal);

    // Close modal
    if (elements.closeBtn) {
        elements.closeBtn.addEventListener('click', closeModal);
    }

    // Click outside to close
    if (elements.modal) {
        elements.modal.addEventListener('click', (e) => {
            if (e.target === elements.modal) {
                closeModal();
            }
        });
    }

    // Escape key to close
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && isModalOpen) {
            closeModal();
        }
    });

    // Toggle button (Start/Stop)
    if (elements.toggleBtn) {
        elements.toggleBtn.addEventListener('click', () => {
            if (isActive) {
                handleStop();
            } else {
                handleStart();
            }
        });
    }

    // Device selection change
    if (elements.deviceSelect) {
        elements.deviceSelect.addEventListener('change', handleDeviceChange);
    }

    // Advanced toggle
    if (elements.advancedToggle) {
        elements.advancedToggle.addEventListener('click', toggleAdvanced);
    }

    // Quick-start buttons
    if (elements.quickStartBackendBtn) {
        elements.quickStartBackendBtn.addEventListener('click', () => handleQuickStart('backend'));
    }
    if (elements.quickStartUdpBtn) {
        elements.quickStartUdpBtn.addEventListener('click', () => handleQuickStart('udp'));
    }

    // Slider change handlers - update value display + immediate apply when active
    setupSlider('recognitionInterval', 's', 'recognition_interval');
    setupSlider('captureDuration', 's', 'capture_duration');
    setupSlider('latencyOffset', 's', 'latency_offset');
    setupSlider('silenceThreshold', '', 'silence_threshold');

    // Reset button handlers - use loaded config values from settings.json
    document.querySelectorAll('.reset-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const targetId = btn.dataset.target;
            // Convert kebab-case ID to snake_case config key
            const configKey = targetId.replace(/-/g, '_');
            // Use loaded config value, fallback to HTML default if config not loaded
            const defaultValue = currentConfig?.[configKey] ?? btn.dataset.default;
            const input = document.getElementById(targetId);
            if (input) {
                input.value = defaultValue;
                input.dispatchEvent(new Event('input'));
            }
        });
    });

    // Start background polling (slower when modal closed)
    startPolling(5000);

    // Initial status check
    refreshStatus();

    console.log('Audio source module initialized');
}

// Quick-start handler
async function handleQuickStart(mode) {
    if (elements.deviceSelect) {
        elements.deviceSelect.value = 'udp';
    }
    await handleStart('udp');
}

// Setup slider with value display and immediate apply
function setupSlider(baseName, suffix, configKey) {
    const slider = elements[baseName];
    const valueDisplay = elements[baseName + 'Value'];

    if (slider && valueDisplay) {
        // Update display on input
        slider.addEventListener('input', () => {
            valueDisplay.textContent = slider.value + suffix;
        });

        // Apply to backend on change (when user releases slider)
        slider.addEventListener('change', async () => {
            if (isActive && configKey) {
                const value = configKey === 'silence_threshold'
                    ? parseInt(slider.value, 10)
                    : parseFloat(slider.value);
                try {
                    await setAudioRecognitionConfig({ [configKey]: value });
                    console.log(`[AudioSource] Applied ${configKey}: ${value}`);
                } catch (error) {
                    console.error(`Failed to apply ${configKey}:`, error);
                }
            }
        });

        // Initial value
        valueDisplay.textContent = slider.value + suffix;
    }
}

export default {
    init,
    updateAudioLevel,
    refreshStatus
};

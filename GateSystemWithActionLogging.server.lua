script.Parent.Values.GateNumber.Value = script.Parent.Name
script.CamAnimScript.Value.Value = script.Parent.ScannerModel

local TweenService = game:GetService("TweenService")
local HttpService = game:GetService("HttpService")

-- Set the "WebhookUrl" attribute on this Script to your Discord-compatible webhook URL.
-- HTTP Requests must also be enabled in Game Settings > Security.
local WEBHOOK_URL = script:GetAttribute("WebhookUrl") or ""
local warnedAboutMissingWebhook = false

local function auditLog(player, action)
	local gateNumber = script.Parent.Values.GateNumber.Value
	local actor = string.format(
		"%s (@%s, UserId %d)",
		player.DisplayName,
		player.Name,
		player.UserId
	)
	local outputMessage = string.format(
		"[Gate Audit] %s | By: %s | Gate: %s",
		action,
		actor,
		tostring(gateNumber)
	)

	-- Log every action to the Roblox server output.
	print(outputMessage)

	if WEBHOOK_URL == "" then
		if not warnedAboutMissingWebhook then
			warn("[Gate Audit] WebhookUrl is not configured on the Script; webhook logging is disabled.")
			warnedAboutMissingWebhook = true
		end
		return
	end

	-- Do not delay the gate controls while the webhook request is being sent.
	task.spawn(function()
		local requestSucceeded, response = pcall(function()
			return HttpService:RequestAsync({
				Url = WEBHOOK_URL,
				Method = "POST",
				Headers = {
					["Content-Type"] = "application/json",
				},
				Body = HttpService:JSONEncode({
					username = "Gate Audit",
					embeds = {
						{
							title = action,
							color = 3447003,
							fields = {
								{
									name = "Staff member",
									value = actor,
									inline = false,
								},
								{
									name = "Gate",
									value = tostring(gateNumber),
									inline = true,
								},
							},
							timestamp = os.date("!%Y-%m-%dT%H:%M:%SZ"),
						},
					},
				}),
			})
		end)

		if not requestSucceeded then
			warn("[Gate Audit] Webhook request failed:", response)
		elseif not response.Success then
			warn(
				"[Gate Audit] Webhook returned",
				response.StatusCode,
				response.StatusMessage,
				response.Body
			)
		end
	end)
end

local finalCallFadeTween = nil
local finalCallFadeRunning = false

local function StartFinalCallFade(boardScreen)
	local FINAL_RED = Color3.fromRGB(221, 72, 72)
	local FINAL_BLUE = Color3.fromRGB(0, 0, 0)

	if finalCallFadeRunning then return end
	finalCallFadeRunning = true

	task.spawn(function()
		local useBlue = true

		while finalCallFadeRunning and boardScreen and boardScreen.Parent do
			local targetColor = useBlue and FINAL_BLUE or FINAL_RED
			useBlue = not useBlue

			if finalCallFadeTween then
				finalCallFadeTween:Cancel()
			end

			finalCallFadeTween = TweenService:Create(
				boardScreen,
				TweenInfo.new(1.4, Enum.EasingStyle.Sine, Enum.EasingDirection.InOut),
				{ BackgroundColor3 = targetColor }
			)

			finalCallFadeTween:Play()
			finalCallFadeTween.Completed:Wait()
		end
	end)
end

local function StopFinalCallFade()
	finalCallFadeRunning = false

	if finalCallFadeTween then
		finalCallFadeTween:Cancel()
		finalCallFadeTween = nil
	end
end

script.Parent.Monitor.LCD.ClickDetector.MouseClick:Connect(function(player)
	if player:GetRankInGroupAsync(35238318) <= 5 then
		return
	end

	local playerGui = player:FindFirstChildOfClass("PlayerGui")
	if not playerGui then return end

	local ui = playerGui:FindFirstChild("31273123621")

	if ui then
		ui.Holder.LocalScript.FORCECLOSE.Value = true
		task.wait(.6)
		ui:Destroy()
		auditLog(player, "Close screen")
	else
		script.Parent.Monitor.LCD.SurfaceGui.GateClosedScreen.Visible = false
		script.Parent.Monitor.LCD.SurfaceGui.FakeControlsScreen.Visible = true

		local newUi = script.UI:Clone()
		newUi.Parent = playerGui
		newUi.Name = "31273123621"

		newUi.Holder.LocalScript.Values.Value = script.Parent.Values
		auditLog(player, "Open screen")

		local tempclientremote = Instance.new("RemoteEvent")
		tempclientremote.Parent = game.ReplicatedStorage
		tempclientremote.Name = tostring(math.random(192121, 19212121))

		newUi.Holder.LocalScript.Remote.Value = tempclientremote

		tempclientremote.OnServerEvent:Connect(function(plr, x, y)
			if plr ~= player then return end

			if x == "Screen" then
				script.Parent.Values.Screen.Value = y
				script.Parent.GateScreen.Board.SurfaceGui.GateClosedScreen.Visible = not y
				auditLog(player, y and "Screen on" or "Screen off")

			elseif x == "GateStatus" then
				script.Parent.Values.GateStatus.Value = y
				game.Workspace.SERVERINFORMATIONCLIENT.Gate_Status.Value = y

				local boardscreen = script.Parent.GateScreen.Board.SurfaceGui["BOARDING STATUS"]
				boardscreen.STATUS.Text = y

				if y == "Gate Closed" then
					StopFinalCallFade()
					boardscreen.Parent.ADVISORY.THING.Text = ""
					boardscreen.BackgroundColor3 = Color3.fromRGB(221, 72, 72)

				elseif y == "Final Call" then
					boardscreen.Parent.ADVISORY.THING.Text = "Prepare your ID / Passport and Boarding Card"
					StartFinalCallFade(boardscreen)

				else
					StopFinalCallFade()
					boardscreen.BackgroundColor3 = Color3.fromRGB(144, 221, 63)
					boardscreen.Parent.ADVISORY.THING.Text = "Prepare your ID / Passport and Boarding Card"
				end

			elseif x == "Priority" then
				script.Parent.Values.Priority.Value = y
				auditLog(player, (y and "Start" or "Stop") .. " boarding for class Priority")

			elseif x == "Standard" then
				script.Parent.Values.Standard.Value = y
				auditLog(player, (y and "Start" or "Stop") .. " boarding for class Standard")
			end
		end)
	end
end)

local function UPD()
	local sic = game.Workspace:WaitForChild("SERVERINFORMATIONCLIENT")

	script.Parent.GateScreen.Board.SurfaceGui.TOPFRAME.FLIGHTNO.Text = sic.FlightCode.Value
	script.Parent.GateScreen.Board.SurfaceGui.TOPFRAME.ARRIVAL.Text = string.upper(sic.Arrival.Value)

	script.Parent.Monitor.LCD.SurfaceGui.FakeControlsScreen.DESTINATION.Text = string.upper(sic.Arrival.Value)
	script.Parent.Monitor.LCD.SurfaceGui.FakeControlsScreen.FLIGHTNUMBER.Text = string.upper(sic.FlightCode.Value)
	script.Parent.Monitor.LCD.SurfaceGui.FakeControlsScreen.GATESTATUS.Text = string.upper(sic.Gate_Status.Value)
end

UPD()

game.Workspace:WaitForChild("SERVERINFORMATIONCLIENT").Arrival:GetPropertyChangedSignal("Value"):Connect(UPD)
game.Workspace:WaitForChild("SERVERINFORMATIONCLIENT").Gate_Status:GetPropertyChangedSignal("Value"):Connect(UPD)
game.Workspace:WaitForChild("SERVERINFORMATIONCLIENT").FlightCode:GetPropertyChangedSignal("Value"):Connect(UPD)

local function GateScannerSystem(Priority, Standard)
	local Players = game:GetService("Players")
	local RunService = game:GetService("RunService")
	local ReplicatedStorage = game:GetService("ReplicatedStorage")
	local HttpService = game:GetService("HttpService")

	local GROUP_ID = 35238318
	local MIN_RANK = 6

	local Values = Priority.Parent
	local ROOT = Values.Parent

	local Boarded = Values:WaitForChild("Boarded")
	local Remaining = Values:WaitForChild("Remaining")

	local GATE = ROOT
	local TICKET_SCANNER = GATE:WaitForChild("ticketScanner")
	local SENSOR = TICKET_SCANNER:WaitForChild("Sensor")
	local DEST = TICKET_SCANNER:WaitForChild("TeleportPos")

	local CAM_SCRIPT_TEMPLATE = script:WaitForChild("CamAnimScript")
	local QUEUE_CLIENT_TEMPLATE = script:WaitForChild("QueueClient")

	local SCANNER_MODEL =
		script:FindFirstChild("ScannerModel") and script.ScannerModel.Value
		or GATE:FindFirstChild("ScannerModel")
		or TICKET_SCANNER:FindFirstChild("ScannerModel")
		or TICKET_SCANNER

	local UpgradeNotification = require(ReplicatedStorage:WaitForChild("BL_NotificationSystem"))

	local ENTER_RADIUS = 12
	local EXIT_RADIUS = 15
	local CHECK_DT = 0.15
	local SCAN_LOCK_TIME = 6.2
	local SERVER_BEEP_DELAY = 4.15
	local SCAN_RESULT_DELAY = 4.35
	local TELEPORT_OFFSET = CFrame.new(0, 3, 0)

	local QUEUE_SCAN_SECONDS = 1.5
	local QUEUE_WALKSPEED = 6
	local QUEUE_JUMPHEIGHT = 1

	local ENTER_R2 = ENTER_RADIUS * ENTER_RADIUS
	local EXIT_R2 = EXIT_RADIUS * EXIT_RADIUS

	local GATE_ID = ROOT:GetAttribute("GateQueueId")
	if not GATE_ID then
		GATE_ID = HttpService:GenerateGUID(false)
		ROOT:SetAttribute("GateQueueId", GATE_ID)
	end

	local playerInZone = {}
	local playerScanLockedUntil = {}
	local playerScannedFirstTime = {}
	local passportNotifyCooldown = {}

	local TOOL_ENABLED = {
		["Fast Track"] = false,
		["Family Fast Track"] = false,
		["Standard"] = false,
	}

	local queueState = {
		Standard = {},
		Priority = {},
	}

	local playerQueue = {}
	local savedMovement = {}

	local QUEUES_FOLDER = ROOT:WaitForChild("Queues")

	local function getOrCreateInt(parent, name)
		local value = parent:FindFirstChild(name)

		if not value then
			value = Instance.new("IntValue")
			value.Name = name
			value.Parent = parent
		end

		return value
	end

	local function updateServerBoardingInfo()
		local sic = workspace:FindFirstChild("SERVERINFORMATIONCLIENT")
		if not sic then return end

		getOrCreateInt(sic, "Boarded").Value = tonumber(Boarded.Value) or 0
		getOrCreateInt(sic, "Remaining").Value = tonumber(Remaining.Value) or 0
	end

	local function isStaff(player)
		local ok, rank = pcall(function()
			return player:GetRankInGroup(GROUP_ID)
		end)

		return ok and rank >= MIN_RANK
	end

	local function updateTicketToggles()
		TOOL_ENABLED["Fast Track"] = Priority.Value
		TOOL_ENABLED["Family Fast Track"] = Priority.Value
		TOOL_ENABLED["Standard"] = Standard.Value
	end

	local function updateCounts()
		local sic = workspace:FindFirstChild("SERVERINFORMATIONCLIENT")
		if not sic then
			Remaining.Value = 0
			updateServerBoardingInfo()
			return
		end

		local checkedFolder = sic:FindFirstChild("CheckedInPlayers")
		if not checkedFolder then
			Remaining.Value = 0
			updateServerBoardingInfo()
			return
		end

		local checkedInNonStaff = 0

		for _, folder in ipairs(checkedFolder:GetChildren()) do
			local player = Players:FindFirstChild(folder.Name)

			if player then
				if not isStaff(player) then
					checkedInNonStaff += 1
				end
			else
				checkedInNonStaff += 1
			end
		end

		local boardedCount = tonumber(Boarded.Value) or 0
		Remaining.Value = math.max(checkedInNonStaff - boardedCount, 0)

		updateServerBoardingInfo()
	end

	local function dist2(a, b)
		local d = a - b
		return d.X * d.X + d.Y * d.Y + d.Z * d.Z
	end

	local function getPartFrom(inst)
		if not inst then return nil end
		if inst:IsA("BasePart") then return inst end
		return inst:FindFirstChildWhichIsA("BasePart", true)
	end


	local function getCharacterHumanoidRoot(player)
		local character = player and player.Character
		if not character then return nil, nil, nil end

		local humanoid = character:FindFirstChildOfClass("Humanoid")
		local hrp = character:FindFirstChild("HumanoidRootPart")

		return character, humanoid, hrp
	end

	local function isPlayerSeated(player)
		local _, humanoid = getCharacterHumanoidRoot(player)
		if not humanoid then return false end

		return humanoid.Sit == true or humanoid.SeatPart ~= nil
	end

	local function clearCharacterVelocity(character)
		for _, descendant in ipairs(character:GetDescendants()) do
			if descendant:IsA("BasePart") then
				descendant.AssemblyLinearVelocity = Vector3.zero
				descendant.AssemblyAngularVelocity = Vector3.zero
			end
		end
	end

	local function safePivotCharacter(player, targetCFrame, reason, notifyTitle, notifyMessage)
		local character, humanoid, hrp = getCharacterHumanoidRoot(player)

		if not character or not humanoid or not hrp then
			return false
		end

		if humanoid.Sit or humanoid.SeatPart then
			warn("[GateScanner] Blocked teleport for seated player:", player.Name, reason or "unknown")

			if notifyTitle and notifyMessage then
				UpgradeNotification.Notify(player, notifyTitle, notifyMessage, 4)
			end

			return false
		end

		clearCharacterVelocity(character)

		local ok, err = pcall(function()
			character:PivotTo(targetCFrame)
		end)

		if not ok then
			warn("[GateScanner] Failed to teleport", player.Name, reason or "unknown", err)
			return false
		end

		clearCharacterVelocity(character)
		return true
	end

	local function getQueueRig(queueName)
		local folder = QUEUES_FOLDER:FindFirstChild(queueName)
		if not folder then return nil end

		return {
			Folder = folder,
			Prompt = folder:FindFirstChild("Prompt"),

			TeleportIntoQueue =
				folder:FindFirstChild("TeleportIntoQueue")
				or folder:FindFirstChild("TelportIntoQueue")
				or folder:FindFirstChild("Teleport into initial")
				or folder:FindFirstChild("TeleportIntoInitial")
				or folder:FindFirstChild("TeleportInitial"),

			ExitQueue = folder:FindFirstChild("ExitQueue"),
		}
	end

	local function getEquippedTool(character)
		for _, child in ipairs(character:GetChildren()) do
			if child:IsA("Tool") then
				return child
			end
		end

		return nil
	end

	local function playerHasFastTrack(player)
		local sic = workspace:FindFirstChild("SERVERINFORMATIONCLIENT")
		if not sic then return false end

		local checkedInPlayers = sic:FindFirstChild("CheckedInPlayers")
		if not checkedInPlayers then return false end

		local playerFolder = checkedInPlayers:FindFirstChild(player.Name)
		if not playerFolder then return false end

		local upgrades = playerFolder:FindFirstChild("Upgrades")
		if not upgrades then return false end

		local priorityBoarding = upgrades:FindFirstChild("PriorityBoarding")

		return priorityBoarding
			and priorityBoarding:IsA("BoolValue")
			and priorityBoarding.Value == true
	end

	local function notifyScanPhone(player)
		local now = os.clock()

		if passportNotifyCooldown[player] and now - passportNotifyCooldown[player] < 4 then
			return
		end

		passportNotifyCooldown[player] = now

		UpgradeNotification.Notify(
			player,
			"Boarding Pass Required",
			"Please scan your phone to board.",
			4
		)
	end

	local function getValidScanTool(player)
		local character = player.Character
		if not character then return nil end

		local tool = getEquippedTool(character)
		if not tool then return nil end

		local boardingEnabled =
			TOOL_ENABLED["Standard"] == true
			or TOOL_ENABLED["Fast Track"] == true
			or TOOL_ENABLED["Family Fast Track"] == true

		if tool.Name == "PASSPORT" or tool.Name == "Passport" then
			if boardingEnabled then
				notifyScanPhone(player)
			end

			return nil
		end

		if tool.Name == "iPhone" then
			if playerScannedFirstTime[player] then
				return nil
			end

			if TOOL_ENABLED["Standard"] == true then
				return tool, "Phone"
			end

			if TOOL_ENABLED["Fast Track"] == true or TOOL_ENABLED["Family Fast Track"] == true then
				if playerHasFastTrack(player) then
					return tool, "Phone"
				end
			end

			return nil
		end

		if TOOL_ENABLED[tool.Name] == true then
			return tool, "Ticket"
		end

		return nil
	end

	local function playerOwnsNamedThing(player, names)
		local function scan(container)
			if not container then return nil end

			for _, name in ipairs(names) do
				local found = container:FindFirstChild(name)
				if found then
					return found
				end
			end

			return nil
		end

		return scan(player.Character) or scan(player:FindFirstChildOfClass("Backpack"))
	end

	local function getQueuedScanProp(player)
		if playerOwnsNamedThing(player, { "iPhone" }) then
			return "Phone"
		end

		if playerOwnsNamedThing(player, { "Fast Track", "Family Fast Track", "Standard" }) then
			return "Ticket"
		end

		return "Phone"
	end

	local function isScanLocked(player)
		local untilTime = playerScanLockedUntil[player]
		return untilTime and time() < untilTime
	end

	local function lockScan(player)
		playerScanLockedUntil[player] = time() + SCAN_LOCK_TIME
	end

	local function playScannerBeep(soundName)
		local sound =
			SCANNER_MODEL:FindFirstChild(soundName, true)
			or TICKET_SCANNER:FindFirstChild(soundName, true)
			or ROOT:FindFirstChild(soundName, true)

		if sound and sound:IsA("Sound") then
			sound:Play()
		end
	end

	local function getPlayerGateFolder(player)
		local sic = workspace:FindFirstChild("SERVERINFORMATIONCLIENT")
		if not sic then return nil end

		local checked = sic:FindFirstChild("CheckedInPlayers")
		if checked and checked:FindFirstChild(player.Name) then
			return checked[player.Name]
		end

		return sic:FindFirstChild(player.Name)
	end

	local function isOffloadApproved(player)
		local folder = getPlayerGateFolder(player)
		if not folder then return false end

		local offload = folder:FindFirstChild("Offload")
		if not offload then return false end

		local approved = offload:FindFirstChild("Offload_APPROVED")
		return approved and approved:IsA("BoolValue") and approved.Value == true
	end

	local function notifyGateRejected(player)
		local rmt6 = ReplicatedStorage:WaitForChild("748173281738219738127318283718921")

		local idAttr = ROOT:GetAttribute("SuitcaseId")
			or SCANNER_MODEL:GetAttribute("SuitcaseId")

		local id = (type(idAttr) == "string" and idAttr ~= "")
			and idAttr
			or tostring(math.random(100000, 999999999))

		local gateNumber = Values:FindFirstChild("GateNumber")
			and Values.GateNumber.Value
			or ROOT.Name

		rmt6:FireAllClients(id, player.Name .. " rejected at gate " .. tostring(gateNumber))
	end

	local function injectScannerCam(player, propName)
		local playerGui = player:FindFirstChildOfClass("PlayerGui")
		if not playerGui then return end

		if not SCANNER_MODEL or not SCANNER_MODEL:IsA("Model") then
			warn("[GateScanner] Invalid SCANNER_MODEL")
			return
		end

		local old = playerGui:FindFirstChild("CamAnimScript")
		if old then
			old:Destroy()
		end

		local ls = CAM_SCRIPT_TEMPLATE:Clone()
		ls.Name = "CamAnimScript"
		ls.Enabled = false

		local scannerOV = ls:FindFirstChild("ScannerModel")
		if not scannerOV then
			scannerOV = Instance.new("ObjectValue")
			scannerOV.Name = "ScannerModel"
			scannerOV.Parent = ls
		end

		scannerOV.Value = SCANNER_MODEL

		local propSV = ls:FindFirstChild("Prop")
		if not propSV then
			propSV = Instance.new("StringValue")
			propSV.Name = "Prop"
			propSV.Parent = ls
		end

		propSV.Value = propName

		ls.Parent = playerGui

		task.defer(function()
			if ls and ls.Parent then
				ls.Enabled = true
			end
		end)
	end

	local function teleportPlayer(player)
		return safePivotCharacter(
			player,
			DEST.CFrame * TELEPORT_OFFSET,
			"scanner completion",
			"Boarding Cancelled",
			"You cannot be boarded while seated."
		)
	end

	local function onPlayerScannedFirstTime(player)
		local module = game.ServerScriptService:FindFirstChild("Check-In Module")
		if not module then return end

		local seatHighlight = module:FindFirstChild("SeatHighlightSystem")
		if not seatHighlight then return end

		local playerGui = player:FindFirstChildOfClass("PlayerGui")
		if not playerGui then return end

		local old = playerGui:FindFirstChild("SeatHighlightSystem")
		if old then
			old:Destroy()
		end

		local cloned = seatHighlight:Clone()
		cloned.Parent = playerGui
		cloned.Enabled = true
	end

	local function onPlayerScanned(player, toolName, propName)
		print("[GateScanner] Scanned:", player.Name, toolName, propName)

		if not playerScannedFirstTime[player] then
			playerScannedFirstTime[player] = true

			Boarded.Value = (tonumber(Boarded.Value) or 0) + 1
			updateCounts()
			updateServerBoardingInfo()

			onPlayerScannedFirstTime(player)
		end
	end

	local function getQueueClient(player)
		local playerGui = player:FindFirstChildOfClass("PlayerGui")
		if not playerGui then return nil end

		return playerGui:FindFirstChild("GateQueueClient_" .. GATE_ID)
	end

	local function ensureQueueClient(player)
		local playerGui = player:FindFirstChildOfClass("PlayerGui")
		if not playerGui then return nil end

		local existing = getQueueClient(player)
		if existing then
			return existing
		end

		local client = QUEUE_CLIENT_TEMPLATE:Clone()
		client.Name = "GateQueueClient_" .. GATE_ID
		client.Enabled = false
		client:SetAttribute("GateId", GATE_ID)

		local updateRemote = Instance.new("RemoteEvent")
		updateRemote.Name = "UpdateQueueGui"
		updateRemote.Parent = client

		local leaveRemote = Instance.new("RemoteEvent")
		leaveRemote.Name = "LeaveQueue"
		leaveRemote.Parent = client

		leaveRemote.OnServerEvent:Connect(function(plr)
			if plr ~= player then return end
			local queueName = playerQueue[player]
			if not queueName then return end

			local queue = queueState[queueName]

			for i = #queue, 1, -1 do
				if queue[i] == player then
					table.remove(queue, i)
				end
			end

			playerQueue[player] = nil

			local saved = savedMovement[player]
			if saved then
				local char = player.Character
				local hum = char and char:FindFirstChildOfClass("Humanoid")

				if hum then
					hum.WalkSpeed = saved.WalkSpeed
					hum.UseJumpPower = saved.UseJumpPower
					hum.JumpPower = saved.JumpPower
					hum.JumpHeight = saved.JumpHeight
				end

				savedMovement[player] = nil
			end

			local rig = getQueueRig(queueName)
			local exitPart = rig and getPartFrom(rig.ExitQueue)

			if exitPart then
				safePivotCharacter(
					player,
					exitPart.CFrame + Vector3.new(0, 3, 0),
					"leave queue",
					"Queue Exit Blocked",
					"You cannot be moved while seated."
				)
			end

			updateRemote:FireClient(player, "HIDE", {})

			for i, queuedPlayer in ipairs(queue) do
				local otherClient = getQueueClient(queuedPlayer)
				local otherRemote = otherClient and otherClient:FindFirstChild("UpdateQueueGui")

				if otherRemote then
					otherRemote:FireClient(queuedPlayer, "SHOW", {
						queueName = queueName,
						position = i,
					})
				end
			end
		end)

		client.Parent = playerGui
		client.Enabled = true

		return client
	end

	local function fireQueueGui(player, action, data)
		local client = ensureQueueClient(player)
		if not client then return end

		local remote = client:FindFirstChild("UpdateQueueGui")
		if remote then
			remote:FireClient(player, action, data or {})
		end
	end

	local function hideQueueGui(player)
		fireQueueGui(player, "HIDE", {})
	end

	local function restoreMovement(player)
		local saved = savedMovement[player]
		if not saved then return end

		local char = player.Character
		local hum = char and char:FindFirstChildOfClass("Humanoid")

		if hum then
			hum.WalkSpeed = saved.WalkSpeed
			hum.UseJumpPower = saved.UseJumpPower
			hum.JumpPower = saved.JumpPower
			hum.JumpHeight = saved.JumpHeight
		end

		savedMovement[player] = nil
	end

	local function setMovementForQueue(player)
		local char = player.Character
		local hum = char and char:FindFirstChildOfClass("Humanoid")
		if not hum then return end

		if not savedMovement[player] then
			savedMovement[player] = {
				WalkSpeed = hum.WalkSpeed,
				JumpPower = hum.JumpPower,
				JumpHeight = hum.JumpHeight,
				UseJumpPower = hum.UseJumpPower,
			}
		end

		hum.WalkSpeed = QUEUE_WALKSPEED

		if hum.UseJumpPower then
			hum.JumpPower = 1
		else
			hum.JumpHeight = QUEUE_JUMPHEIGHT
		end
	end

	local updateAllQueuePositions

	local function removeFromQueue(player, teleportToExit)
		local queueName = playerQueue[player]
		if not queueName then
			hideQueueGui(player)
			return
		end

		local queue = queueState[queueName]

		for i = #queue, 1, -1 do
			if queue[i] == player then
				table.remove(queue, i)
			end
		end

		playerQueue[player] = nil
		restoreMovement(player)
		hideQueueGui(player)

		if teleportToExit then
			local rig = getQueueRig(queueName)
			local exitPart = rig and getPartFrom(rig.ExitQueue)

			if exitPart then
				safePivotCharacter(
					player,
					exitPart.CFrame + Vector3.new(0, 3, 0),
					"leave queue",
					"Queue Exit Blocked",
					"You cannot be moved while seated."
				)
			end
		end

		updateAllQueuePositions(queueName)
	end

	updateAllQueuePositions = function(queueName)
		for i, player in ipairs(queueState[queueName]) do
			if player and player.Parent then
				fireQueueGui(player, "SHOW", {
					queueName = queueName,
					position = i,
				})
			end
		end
	end

	local function queuesScreenEnabled()
		return Values:FindFirstChild("Screen") and Values.Screen.Value == true
	end

	local function addToQueue(player, queueName)
		if playerQueue[player] then
			updateAllQueuePositions(playerQueue[player])
			return
		end

		if not queuesScreenEnabled() then
			UpgradeNotification.Notify(
				player,
				"Gate Closed",
				"Boarding is not currently open.",
				4
			)
			return
		end

		if queueName == "Priority" and not playerHasFastTrack(player) then
			UpgradeNotification.Notify(
				player,
				"Priority Boarding",
				"You do not have priority boarding.",
				4
			)
			return
		end

		if isPlayerSeated(player) then
			playerQueue[player] = nil
			hideQueueGui(player)
			restoreMovement(player)

			UpgradeNotification.Notify(
				player,
				"Queue Cancelled",
				"You cannot join the queue while seated.",
				4
			)

			return
		end

		local rig = getQueueRig(queueName)
		if not rig then
			warn("[GateQueue] Missing queue:", queueName)
			hideQueueGui(player)
			return
		end

		local teleportPart = getPartFrom(rig.TeleportIntoQueue)

		if not teleportPart then
			warn("[GateQueue] Missing TeleportIntoQueue for", queueName)
			hideQueueGui(player)
			return
		end

		local character = player.Character
		local hrp = character and character:FindFirstChild("HumanoidRootPart")

		if not hrp then
			hideQueueGui(player)
			return
		end

		local distance = (hrp.Position - teleportPart.Position).Magnitude

		if distance > 10 then
			playerQueue[player] = nil
			hideQueueGui(player)
			restoreMovement(player)

			UpgradeNotification.Notify(
				player,
				"Queue Cancelled",
				"You moved too far away from the queue.",
				4
			)

			return
		end

		local teleported = safePivotCharacter(
			player,
			teleportPart.CFrame + Vector3.new(0, 3, 0),
			"join queue",
			"Queue Cancelled",
			"You cannot join the queue while seated."
		)

		if not teleported then
			playerQueue[player] = nil
			hideQueueGui(player)
			restoreMovement(player)
			return
		end

		table.insert(queueState[queueName], player)
		playerQueue[player] = queueName

		setMovementForQueue(player)
		updateAllQueuePositions(queueName)
	end

	local function scanPlayer(player, tool, propName)
		if isScanLocked(player) then return end

		lockScan(player)
		injectScannerCam(player, propName)

		if isOffloadApproved(player) then
			task.delay(SERVER_BEEP_DELAY, function()
				playScannerBeep("beep_decline")
			end)

			task.delay(SCAN_RESULT_DELAY, function()
				if not player or not player.Parent then return end
				notifyGateRejected(player)
			end)

			return
		end

		task.delay(SERVER_BEEP_DELAY, function()
			playScannerBeep("beep_success")
		end)

		task.delay(SCAN_RESULT_DELAY, function()
			if not player or not player.Parent then return end
			if teleportPlayer(player) then
				onPlayerScanned(player, tool.Name, propName)
			end
		end)
	end

	local function scanQueuedPlayer(player, propName)
		if isScanLocked(player) then return end

		lockScan(player)
		hideQueueGui(player)
		injectScannerCam(player, propName)

		if isOffloadApproved(player) then
			task.delay(SERVER_BEEP_DELAY, function()
				playScannerBeep("beep_decline")
			end)

			task.delay(SCAN_RESULT_DELAY, function()
				if not player or not player.Parent then return end
				notifyGateRejected(player)
			end)

			return
		end

		task.delay(SERVER_BEEP_DELAY, function()
			playScannerBeep("beep_success")
		end)

		task.delay(SCAN_RESULT_DELAY, function()
			if not player or not player.Parent then return end
			if teleportPlayer(player) then
				onPlayerScanned(player, propName, propName)
			end
		end)
	end

	local allQueuePrompts = {}

	local function setAllQueuePromptsEnabled(enabled)
		for _, prompt in ipairs(allQueuePrompts) do
			if prompt and prompt.Parent then
				prompt.Enabled = enabled == true
			end
		end
	end

	local function createCustomPrompt(promptPart, queueName)
		local part = getPartFrom(promptPart)
		if not part then return end

		for _, child in ipairs(part:GetChildren()) do
			if child:IsA("ProximityPrompt") then
				child:Destroy()
			end
		end

		local prompt = Instance.new("ProximityPrompt")
		prompt.Name = "GateCustomPrompt"
		prompt.ActionText = ""
		prompt.ObjectText = ""
		prompt.Style = Enum.ProximityPromptStyle.Custom
		prompt.KeyboardKeyCode = Enum.KeyCode.E
		prompt.GamepadKeyCode = Enum.KeyCode.ButtonX
		prompt.ClickablePrompt = true
		prompt.HoldDuration = 0.85
		prompt.RequiresLineOfSight = false
		prompt.MaxActivationDistance = 10
		prompt.Enabled = queuesScreenEnabled()

		prompt:SetAttribute("GateId", GATE_ID)
		prompt:SetAttribute("QueueName", queueName)
		prompt:SetAttribute("Title", queueName == "Priority" and "PRIORITY BOARDING" or "STANDARD BOARDING")
		prompt:SetAttribute("SubTitle", queueName == "Priority" and "Join priority queue" or "Join standard queue")

		prompt.Parent = part
		table.insert(allQueuePrompts, prompt)

		prompt.Triggered:Connect(function(player)
			addToQueue(player, queueName)
		end)
	end

	local function hookQueuePrompts()
		local standardRig = getQueueRig("Standard")
		local priorityRig = getQueueRig("Priority")

		if standardRig then
			createCustomPrompt(standardRig.Prompt, "Standard")
		end

		if priorityRig then
			createCustomPrompt(priorityRig.Prompt, "Priority")
		end
	end

	local function processQueue(queueName)
		while true do
			task.wait(QUEUE_SCAN_SECONDS)

			local enabled =
				queuesScreenEnabled()
				and (
					(queueName == "Priority" and Priority.Value == true)
					or (queueName == "Standard" and Standard.Value == true)
				)

			if not enabled then
				continue
			end

			local queue = queueState[queueName]
			local player = queue[1]

			if not player or not player.Parent then
				table.remove(queue, 1)
				updateAllQueuePositions(queueName)
				continue
			end

			table.remove(queue, 1)
			playerQueue[player] = nil
			restoreMovement(player)
			hideQueueGui(player)

			if isPlayerSeated(player) then
				UpgradeNotification.Notify(
					player,
					"Boarding Cancelled",
					"You cannot be boarded while seated.",
					4
				)
				updateAllQueuePositions(queueName)
				continue
			end

			local propName = getQueuedScanProp(player)

			scanQueuedPlayer(player, propName)
			updateAllQueuePositions(queueName)
		end
	end

	Priority.Changed:Connect(updateTicketToggles)
	Standard.Changed:Connect(updateTicketToggles)

	if Values:FindFirstChild("Screen") then
		Values.Screen:GetPropertyChangedSignal("Value"):Connect(function()
			setAllQueuePromptsEnabled(queuesScreenEnabled())

			if not queuesScreenEnabled() then
				for _, queueName in ipairs({ "Standard", "Priority" }) do
					local queue = queueState[queueName]

					for i = #queue, 1, -1 do
						local player = queue[i]
						if player then
							removeFromQueue(player, true)
						end
					end
				end
			end
		end)
	end

	local function hookCheckedInFolder()
		local sic = workspace:WaitForChild("SERVERINFORMATIONCLIENT")
		local checkedFolder = sic:WaitForChild("CheckedInPlayers")

		checkedFolder.ChildAdded:Connect(function()
			updateCounts()
			updateServerBoardingInfo()
		end)

		checkedFolder.ChildRemoved:Connect(function()
			updateCounts()
			updateServerBoardingInfo()
		end)

		updateCounts()
		updateServerBoardingInfo()
	end

	task.spawn(hookCheckedInFolder)

	Players.PlayerAdded:Connect(function(player)
		task.defer(function()
			ensureQueueClient(player)
			updateCounts()
			updateServerBoardingInfo()
		end)

		player.CharacterAdded:Connect(function()
			task.wait(1)

			ensureQueueClient(player)

			if playerQueue[player] then
				setMovementForQueue(player)
				updateAllQueuePositions(playerQueue[player])
			end
		end)
	end)

	for _, player in ipairs(Players:GetPlayers()) do
		task.defer(function()
			ensureQueueClient(player)
		end)
	end

	Players.PlayerRemoving:Connect(function(player)
		removeFromQueue(player, false)

		playerInZone[player] = nil
		playerScanLockedUntil[player] = nil
		playerScannedFirstTime[player] = nil
		passportNotifyCooldown[player] = nil

		task.defer(function()
			updateCounts()
			updateServerBoardingInfo()
		end)
	end)

	Boarded.Changed:Connect(function()
		updateCounts()
		updateServerBoardingInfo()
	end)

	Remaining.Changed:Connect(updateServerBoardingInfo)

	updateTicketToggles()
	updateCounts()
	updateServerBoardingInfo()
	hookQueuePrompts()
	setAllQueuePromptsEnabled(queuesScreenEnabled())

	task.spawn(function()
		processQueue("Priority")
	end)

	task.spawn(function()
		processQueue("Standard")
	end)

	local acc = 0

	RunService.Heartbeat:Connect(function(dt)
		acc += dt

		if acc < CHECK_DT then
			return
		end

		acc = 0
		updateTicketToggles()

		for _, player in ipairs(Players:GetPlayers()) do
			if playerQueue[player] then
				continue
			end

			local character = player.Character
			local hrp = character and character:FindFirstChild("HumanoidRootPart")

			if not hrp then
				playerInZone[player] = nil
				continue
			end

			local distanceSquared = dist2(hrp.Position, SENSOR.Position)

			if distanceSquared > EXIT_R2 then
				playerInZone[player] = false
				continue
			end

			if distanceSquared <= ENTER_R2 then
				local tool, propName = getValidScanTool(player)

				if tool then
					if not playerInZone[player] then
						playerInZone[player] = true
						scanPlayer(player, tool, propName)
					elseif not isScanLocked(player) then
						scanPlayer(player, tool, propName)
					end
				end
			end
		end
	end)
end

GateScannerSystem(script.Parent.Values.Priority, script.Parent.Values.Standard)

package com.modsync.updater;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.network.chat.Component;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.client.event.ScreenEvent;

@Mod(value = "modsync", dist = Dist.CLIENT)
public final class UpdaterMod {
    public UpdaterMod() {
        NeoForge.EVENT_BUS.addListener(UpdaterMod::onTitleScreenInit);
    }

    private static void onTitleScreenInit(ScreenEvent.Init.Post event) {
        if (!(event.getScreen() instanceof TitleScreen screen)) return;
        event.addListener(Button.builder(Component.literal("整合包更新"), button ->
                Minecraft.getInstance().setScreen(new UpdaterScreen(screen)))
                .bounds(Math.max(4, screen.width - 110), Math.max(4, screen.height - 48), 104, 20).build());
    }
}

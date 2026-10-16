package com.modsync.updater;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.screen.v1.ScreenEvents;
import net.fabricmc.fabric.api.client.screen.v1.Screens;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.components.AbstractWidget;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.network.chat.Component;

public final class UpdaterMod implements ClientModInitializer {
    @Override
    public void onInitializeClient() {
        ScreenEvents.AFTER_INIT.register((client, screen, scaledWidth, scaledHeight) -> {
            if (!(screen instanceof TitleScreen titleScreen)) return;

            for (AbstractWidget widget : Screens.getWidgets(screen)) {
                if ("整合包更新".equals(widget.getMessage().getString())) {
                    return;
                }
            }

            Screens.getWidgets(screen).add(Button.builder(Component.literal("整合包更新"), button ->
                    Minecraft.getInstance().gui.setScreen(new UpdaterScreen(titleScreen)))
                    .bounds(Math.max(4, screen.width - 110), Math.max(4, screen.height - 48), 104, 20).build());
        });
    }
}
